import pickle
from dataclasses import dataclass

import pytest

from insightpilot.checkpoints import strict_serializer

INVOCATIONS = []


@dataclass
class UnapprovedPayload:
    value: str

    def __post_init__(self):
        INVOCATIONS.append("constructor")


def pickle_constructor():
    INVOCATIONS.append("pickle")
    return {"unapproved": True}


class PicklePayload:
    def __reduce__(self):
        return pickle_constructor, ()


def test_checkpoint_serializer_never_reconstructs_unapproved_module_types():
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    attack = JsonPlusSerializer(allowed_msgpack_modules=True).dumps_typed(
        UnapprovedPayload("synthetic")
    )
    INVOCATIONS.clear()
    try:
        strict_serializer().loads_typed(attack)
    except (ValueError, TypeError, NotImplementedError):
        pass
    assert INVOCATIONS == []


def test_checkpoint_serializer_disables_pickle_even_for_known_blob():
    attack = pickle.dumps(PicklePayload())
    INVOCATIONS.clear()
    with pytest.raises((ValueError, TypeError, NotImplementedError)):
        strict_serializer().loads_typed(("pickle", attack))
    assert INVOCATIONS == []
