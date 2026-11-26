"""Synthetic endpoint payload, shared by offline transport and restart fixtures."""
import json


def contract_json(question="Synthetic task", **changes):
    value = dict(task=question, audience=None, topics=[], examples=[], format=None,
                 deliverables=[], constraints=[], obligations=[question], continuation=False,
                 superseded_requirements=[], kind="answer", minutes=None,
                 outline_only=False, limited_scope=False, local_only=False, file_output=None,
                 literal_constraints=[])
    value.update(changes)
    return json.dumps(value, ensure_ascii=False)
