from __future__ import annotations

from typing import Any

from debug_agent.runtime.contracts import AgentRunRequest, AgentRunResult, RunContext


RUNTIME_SAFETY_PREFIX = (
    "runtime safety: use only runtime-provided tools and do not bypass ToolBroker."
)


class LangChainAgentLoopAdapter:
    def __init__(self, *, model: object, tool_broker: object | None = None) -> None:
        self.model = model
        self.tool_broker = tool_broker
        self._cancelled_runs: set[str] = set()

    def run(self, request: AgentRunRequest, context: RunContext) -> AgentRunResult:
        if request.run_id in self._cancelled_runs:
            return _error_result("cancelled", "cancelled", "Run was cancelled.")
        messages = _compose_messages(request)
        try:
            response = self.model.invoke(messages)
        except TimeoutError as exc:
            return _error_result("timeout", "timeout", str(exc), source="model")
        except KeyboardInterrupt as exc:
            return _error_result("cancelled", "cancelled", str(exc), source="model")
        except Exception as exc:
            return _error_result("failed", "model_error", str(exc), source="model")

        assistant_output = _response_content(response)
        tool_results = self._invoke_tool_calls(request, context, response)
        return AgentRunResult(
            status="completed",
            assistant_output=assistant_output,
            tool_results=tool_results,
            usage=getattr(response, "usage", {}) or {},
            error=None,
            metadata={},
        )

    def cancel(self, run_id: str) -> None:
        self._cancelled_runs.add(run_id)

    def _invoke_tool_calls(
        self, request: AgentRunRequest, context: RunContext, response: object
    ) -> list[dict[str, Any]]:
        tool_calls = getattr(response, "tool_calls", []) or []
        if not tool_calls:
            return []
        if self.tool_broker is None:
            raise RuntimeError("Tool calls require ToolBroker")
        context_dict = {
            "workspace_root": context.workspace_root,
            "artifact_root": context.artifact_root,
            "approval_mode": context.approval_mode,
            "cancellation_token": context.cancellation_token,
            "timeout_seconds": request.timeout_seconds,
            "metadata": context.metadata,
        }
        results = []
        for call in tool_calls:
            result = self.tool_broker.invoke(
                request.session_id,
                request.run_id,
                call["name"],
                call.get("args", {}),
                context_dict,
            )
            results.append(result.to_dict())
        return results


def _compose_messages(request: AgentRunRequest) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": f"{RUNTIME_SAFETY_PREFIX}\n\n{request.system_prompt}",
        },
        *request.conversation,
        {"role": "user", "content": request.user_input},
    ]


def _response_content(response: object) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    return str(content)


def _error_result(
    status: str,
    error_class: str,
    message: str,
    *,
    source: str = "adapter",
) -> AgentRunResult:
    return AgentRunResult(
        status=status,
        assistant_output=None,
        tool_results=[],
        usage={},
        error={
            "error_class": error_class,
            "message": message,
            "source": source,
            "recoverable": True,
        },
        metadata={},
    )
