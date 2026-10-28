from langgraph.graph import END, START, StateGraph

from insightpilot.agent.nodes import AgentNodes
from insightpilot.agent.state import AgentState


def build_graph(model, tools, saver):
    nodes = AgentNodes(model, tools)
    builder = StateGraph(AgentState)
    for name in (
        "bootstrap",
        "plan",
        "clarification",
        "generate",
        "validate",
        "repair",
        "cost_review",
        "execute",
        "describe",
    ):
        builder.add_node(name, nodes.guarded(getattr(nodes, name)))

    def route(function):
        return lambda state: END if state.get("status") == "failed" else function(state)

    builder.add_edge(START, "bootstrap")
    builder.add_conditional_edges("bootstrap", route(lambda s: "plan"))
    builder.add_conditional_edges(
        "plan", route(lambda s: "clarification" if s["plan"]["needs_clarification"] else "generate")
    )
    builder.add_conditional_edges("clarification", route(lambda s: "plan"))
    builder.add_conditional_edges("generate", route(lambda s: "validate"))
    builder.add_conditional_edges(
        "validate",
        route(
            lambda s: (
                "repair"
                if s["status"] == "repairing"
                else ("cost_review" if s["requires_approval"] else "execute")
            )
        ),
    )
    builder.add_conditional_edges("repair", route(lambda s: "generate"))
    builder.add_conditional_edges("cost_review", route(lambda s: "execute"))
    builder.add_conditional_edges(
        "execute",
        route(
            lambda s: (
                "repair"
                if s["status"] == "repairing"
                else ("generate" if s["step_index"] < len(s["plan"]["steps"]) else "describe")
            )
        ),
    )
    builder.add_edge("describe", END)
    return builder.compile(checkpointer=saver)
