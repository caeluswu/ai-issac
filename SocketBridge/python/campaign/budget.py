"""Conservative request reservations, shared by every HTTP attempt."""
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock
import json

diagnostic_request = ContextVar("diagnostic_request",default=False)


class BudgetExceeded(RuntimeError): pass


@dataclass
class RequestBudget:
    max_requests: int = 6
    max_routine: int = 4
    max_diagnostic: int = 2
    max_input: int = 60000
    max_output: int = 12000
    requests: int = 0
    routine: int = 0
    diagnostic: int = 0
    input_reserved: int = 0
    output_reserved: int = 0
    input_actual: int = 0
    output_actual: int = 0
    usage_known: int = 0
    _lock: object = field(default_factory=Lock,repr=False)

    def reserve(self, messages, max_output=4096):
        with self._lock: return self._reserve(messages,max_output)

    def _reserve(self, messages, max_output):
        # UTF-8 bytes conservatively bound text tokens; add chat framing.
        estimate = len(json.dumps(messages,ensure_ascii=False).encode("utf-8"))+32*len(messages)+64
        diagnostic = diagnostic_request.get()
        if self.requests >= self.max_requests or (self.diagnostic >= self.max_diagnostic if diagnostic else self.routine >= self.max_routine):
            raise BudgetExceeded("LLM_REQUEST_BUDGET_EXHAUSTED")
        if self.input_reserved+estimate > self.max_input: raise BudgetExceeded("LLM_INPUT_BUDGET_EXHAUSTED")
        output = min(max_output,self.max_output-self.output_reserved)
        if output < 256: raise BudgetExceeded("LLM_OUTPUT_BUDGET_EXHAUSTED")
        self.requests += 1
        if diagnostic: self.diagnostic += 1
        else: self.routine += 1
        self.input_reserved += estimate
        self.output_reserved += output
        return {"input":estimate,"output":output,"settled":False}

    def settle(self, ticket, usage):
        with self._lock: return self._settle(ticket,usage)

    def _settle(self, ticket, usage):
        if ticket["settled"]: raise ValueError("REQUEST_ALREADY_SETTLED")
        ticket["settled"] = True
        prompt, completion = usage.get("prompt_tokens"),usage.get("completion_tokens")
        if type(prompt) is int and type(completion) is int and prompt >= 0 and completion >= 0:
            self.input_actual += prompt; self.output_actual += completion; self.usage_known += 1
            self.input_reserved += prompt-ticket["input"]
            self.output_reserved += completion-ticket["output"]
            if self.input_reserved > self.max_input or self.output_reserved > self.max_output:
                raise BudgetExceeded("LLM_USAGE_EXCEEDED_RESERVATION")

    def report(self):
        return {**{k:v for k,v in self.__dict__.items() if not k.startswith("_")},"input_estimator":"utf8_bytes_plus_framing_upper_bound",
                "unknown_usage_attempts":self.requests-self.usage_known,"cost":None}
