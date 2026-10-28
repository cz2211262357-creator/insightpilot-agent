"A real minimal graph shared by parent and two independent process invocations."

from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt


class State(TypedDict):
    counter: int
    query_id: str
    approved: bool


def graph(saver):
    def calculate(state):
        return {"counter": state["counter"] + 1}

    def approval(state):
        result = interrupt(
            {"query_id": state["query_id"], "reason": "Synthetic expensive readonly query"}
        )
        return {"approved": result.get("approved") is True}

    builder = StateGraph(State)
    builder.add_node("calculate", calculate)
    builder.add_node("approval", approval)
    builder.add_edge(START, "calculate")
    builder.add_edge("calculate", "approval")
    builder.add_edge("approval", END)
    return builder.compile(checkpointer=saver)
