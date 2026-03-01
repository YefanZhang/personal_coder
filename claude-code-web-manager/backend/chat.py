import asyncio
import json
import os
import shutil
from typing import Callable, Awaitable, Optional


class ChatSession:
    """Manages an interactive Claude Code chat session.

    Each session maintains conversation continuity via --resume <session_id>.
    Messages are sent one at a time; each spawns a new claude subprocess.
    """

    def __init__(self, working_dir: str):
        self.working_dir = working_dir
        self.session_id: Optional[str] = None
        self.process: Optional[asyncio.subprocess.Process] = None
        self.is_processing = False

    def _build_env(self) -> dict[str, str]:
        env = {**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
        env.pop("CLAUDECODE", None)
        return env

    async def send_message(
        self,
        text: str,
        on_text: Callable[[str], Awaitable[None]],
        on_tool: Callable[[str], Awaitable[None]],
        on_session_info: Callable[[str], Awaitable[None]],
        on_done: Callable[[dict], Awaitable[None]],
        on_error: Callable[[str], Awaitable[None]],
    ) -> None:
        if self.is_processing:
            await on_error("A message is already being processed")
            return

        claude_path = shutil.which("claude")
        if not claude_path:
            await on_error("claude CLI not found in PATH")
            return

        self.is_processing = True

        cmd = [
            claude_path,
            "-p", text,
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        if self.session_id:
            cmd.extend(["--resume", self.session_id])

        try:
            self.process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=self.working_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._build_env(),
            )

            parsed: dict = {}
            buf = b""

            while True:
                chunk = await self.process.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw_line, buf = buf.split(b"\n", 1)
                    decoded = raw_line.decode().rstrip()
                    if not decoded:
                        continue
                    await self._process_line(
                        decoded, on_text, on_tool, on_session_info, parsed,
                    )

            # Flush trailing data
            if buf.strip():
                decoded = buf.decode().rstrip()
                await self._process_line(
                    decoded, on_text, on_tool, on_session_info, parsed,
                )

            await self.process.wait()
            stderr_bytes = await self.process.stderr.read()
            stderr = stderr_bytes.decode()

            exit_code = self.process.returncode

            # Capture session_id for conversation continuity
            if parsed.get("session_id"):
                self.session_id = parsed["session_id"]

            self.process = None
            self.is_processing = False

            if exit_code != 0 and not parsed.get("result_text"):
                await on_error(stderr or f"claude exited with code {exit_code}")
            else:
                await on_done({
                    "input_tokens": parsed.get("input_tokens"),
                    "output_tokens": parsed.get("output_tokens"),
                    "cost_usd": parsed.get("cost_usd"),
                    "session_id": self.session_id,
                })

        except asyncio.CancelledError:
            self.is_processing = False
            await self._kill_process()
            raise
        except Exception as e:
            self.is_processing = False
            self.process = None
            await on_error(str(e))

    async def _process_line(
        self,
        decoded: str,
        on_text: Callable[[str], Awaitable[None]],
        on_tool: Callable[[str], Awaitable[None]],
        on_session_info: Callable[[str], Awaitable[None]],
        parsed: dict,
    ) -> None:
        try:
            event = json.loads(decoded)
        except json.JSONDecodeError:
            await on_text(decoded)
            return

        event_type = event.get("type")

        if event_type == "assistant":
            msg = event.get("message", {})
            for block in msg.get("content", []):
                if block.get("type") == "text":
                    await on_text(block["text"])
                elif block.get("type") == "tool_use":
                    tool_name = block.get("name", "tool")
                    tool_input = block.get("input", {})
                    summary = f"[Using {tool_name}]"
                    if tool_name == "Bash" and "command" in tool_input:
                        summary = f"[Running: {tool_input['command'][:100]}]"
                    elif tool_name in ("Edit", "Write") and "file_path" in tool_input:
                        summary = f"[{tool_name}: {tool_input['file_path']}]"
                    elif tool_name == "Read" and "file_path" in tool_input:
                        summary = f"[Reading: {tool_input['file_path']}]"
                    await on_tool(summary)

        elif event_type == "result":
            parsed["result_text"] = event.get("result", "")
            parsed["cost_usd"] = event.get("total_cost_usd")
            parsed["session_id"] = event.get("session_id")
            usage = event.get("usage", {})
            parsed["input_tokens"] = usage.get("input_tokens")
            parsed["output_tokens"] = usage.get("output_tokens")

        elif event_type == "system":
            model = event.get("model", "unknown")
            await on_session_info(model)

    async def cancel(self) -> None:
        self.is_processing = False
        await self._kill_process()

    async def _kill_process(self) -> None:
        if self.process:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
            self.process = None

    async def cleanup(self) -> None:
        await self._kill_process()
