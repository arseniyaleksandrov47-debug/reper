"""Read historical floor reports through the current audit types."""

from real_estate_engine.floor_policy import BusinessFloorProfile, FloorAssessment


def load_floor_profile(record):
    retired_fields = {'requirements', 'assumptions', 'context_questions', 'model_used', 'review_notes'}
    data = {key: value for key, value in record.items() if key not in retired_fields}
    for name in ('allowed_floors', 'request_clauses', 'scenario_context_quotes', 'waived_requirement_quotes'):
        if data.get(name) is not None:
            data[name] = tuple(data[name])
    return BusinessFloorProfile(**data)


def load_floor_assessment(record):
    data = {key: value for key, value in record.items() if key not in {'fit_score', 'score_adjustment'}}
    data['floors'] = tuple(data['floors'])
    return FloorAssessment(**data)
