"""
Per-request agent trace. Every LLM call, tool call, and agent decision becomes one step:
  llm   : step name, provider, model, tokens (input / output / cache read / cache write), cost, latency, error
  tool  : tool name, parameters, result summary, latency, error
  event : agent decisions (action chosen, planner reasoning, summary)
summary() rolls the steps up into request metrics: steps, LLM calls, tool calls, tokens, cost, latency.
The trace is emitted inside the single JSON log line per request (pipeline.done), so it
lands in CloudWatch with no extra infrastructure.
"""
import time


class Trace:
    def __init__(self):
        self.t0 = time.time()
        self.steps = []

    def _add(self, kind, name, started=None, /, **data):
        now = time.time()
        step = {"n": len(self.steps) + 1, "type": kind, "name": name,
                "start_ms": int(((started or now) - self.t0) * 1000)}
        if started is not None:
            step["latency_ms"] = int((now - started) * 1000)
        step.update({k: v for k, v in data.items() if v is not None})
        self.steps.append(step)
        return step

    def llm(self, name, provider, model, usage, cost, started, error=None):
        self._add("llm", name, started, provider=provider, model=model,
                  usage=usage, cost_usd=cost, error=error)

    def tool(self, name, params, started, result=None, error=None):
        self._add("tool", name, started, params=params, result=result, error=error)

    def event(self, name, **data):
        self._add("event", name, None, **data)

    def summary(self):
        llm = [s for s in self.steps if s["type"] == "llm"]
        tools = [s for s in self.steps if s["type"] == "tool"]

        def tokens(key):
            return sum((s.get("usage") or {}).get(key, 0) for s in llm)

        return {
            "steps": len(self.steps),
            "llm_calls": len(llm),
            "tool_calls": len(tools),
            "input_tokens": tokens("input_tokens"),
            "output_tokens": tokens("output_tokens"),
            "cache_read_tokens": tokens("cache_read_tokens"),
            "cache_write_tokens": tokens("cache_write_tokens"),
            "cost_usd": round(sum(s.get("cost_usd") or 0 for s in llm), 6),
            "errors": sum(1 for s in self.steps if s.get("error")),
            "latency_ms": int((time.time() - self.t0) * 1000),
        }