from dataclasses import dataclass

from tugboat.scoring import ScoreOutcome, default_score_plugins, score_episode
from tugboat.traces.ingest import ingest_jsonl_trace_as_episode


def test_failed_test_result_event_produces_failed_tests_outcome():
    outcomes = score_episode(
        {
            "events": [
                {
                    "id": "evt-test-1",
                    "type": "test_result",
                    "status": "failed",
                    "summary": "pytest failed",
                }
            ]
        }
    )

    assert outcomes[0].plugin == "tests"
    assert outcomes[0].label == "failed-tests"
    assert outcomes[0].metrics == {"failed_tests": 1}
    assert outcomes[0].evidence == ("evt-test-1",)


def test_default_score_plugins_are_named_roadmap_plugins():
    assert tuple(plugin.name for plugin in default_score_plugins()) == (
        "tests",
        "human",
        "verifier",
        "agent-review",
        "policy",
        "user-correction",
    )


@dataclass(frozen=True)
class HumanDecision:
    id: str
    type: str
    label: str


def test_human_accepted_and_rejected_labels_are_detected_from_dataclasses():
    outcomes = score_episode(
        [
            HumanDecision("evt-human-1", "human_decision", "accepted"),
            HumanDecision("evt-human-2", "human_decision", "rejected"),
        ]
    )

    assert [(outcome.plugin, outcome.label, outcome.evidence) for outcome in outcomes] == [
        ("human", "human-accepted", ("evt-human-1",)),
        ("human", "human-rejected", ("evt-human-2",)),
    ]


def test_severe_agent_review_finding_produces_severity_score():
    outcomes = score_episode(
        {
            "events": [
                {
                    "id": "evt-review-1",
                    "type": "agent_review",
                    "finding": {"severity": "critical", "message": "Deletes safety policy."},
                }
            ]
        }
    )

    assert outcomes[0].plugin == "agent-review"
    assert outcomes[0].label == "agent-review-severe"
    assert outcomes[0].metrics == {"severity_score": 4}
    assert outcomes[0].evidence == ("evt-review-1",)


def test_policy_violation_event_is_detected():
    outcomes = score_episode(
        {
            "events": [
                {
                    "id": "evt-policy-1",
                    "type": "policy_violation",
                    "policy": "network",
                    "severity": "high",
                }
            ]
        }
    )

    assert outcomes[0].plugin == "policy"
    assert outcomes[0].label == "policy-violation"
    assert outcomes[0].metrics == {"violations": 1}
    assert outcomes[0].evidence == ("evt-policy-1",)


def test_repeated_user_corrections_for_similar_text_are_counted():
    outcomes = score_episode(
        {
            "events": [
                {
                    "id": "evt-correction-1",
                    "type": "user_correction",
                    "text": "Please run tests before final.",
                },
                {
                    "id": "evt-correction-2",
                    "type": "user_correction",
                    "text": "Run the tests before the final response.",
                },
                {
                    "id": "evt-correction-3",
                    "type": "user_correction",
                    "text": "Keep the summary short.",
                },
            ]
        }
    )

    recurring = [outcome for outcome in outcomes if outcome.plugin == "user-correction"]

    assert len(recurring) == 1
    assert recurring[0].label == "recurring-user-correction"
    assert recurring[0].metrics == {"recurrence_count": 2}
    assert recurring[0].evidence == ("evt-correction-1", "evt-correction-2")


def test_scoring_accepts_object_episodes_mapping_events_and_fallback_evidence():
    class Episode:
        def __init__(self):
            self.events = {
                "first": {"event_type": "test_result", "passed": False, "evidence_id": "evt-test-2"},
                "second": {"kind": "policy_failure"},
                "third": {"type": "user_correction", "text": ""},
            }

    outcomes = score_episode(Episode())

    assert [(outcome.label, outcome.evidence) for outcome in outcomes] == [
        ("failed-tests", ("evt-test-2",)),
        ("policy-violation", ("unknown",)),
    ]


def test_scoring_accepts_canonical_episode_objects(tmp_path):
    trace = tmp_path / "episode.jsonl"
    trace.write_text('{"type":"test_result","suite":"unit","passed":false}\n', encoding="utf-8")
    episode = ingest_jsonl_trace_as_episode(trace)

    outcomes = score_episode(episode)

    assert outcomes[0].label == "failed-tests"
    assert outcomes[0].evidence[0].startswith("ev_")


def test_scoring_uses_canonical_outcome_label_and_verifier_evidence(tmp_path):
    episode = {
        "events": [
            {
                "type": "outcome_label",
                "label": "rejected",
                "source_trust": "verifier",
                "evidence_id": "ev_label",
            },
            {
                "type": "verifier_score",
                "name": "governance",
                "score": 0.25,
                "source_trust": "verifier",
                "evidence_id": "ev_score",
            },
        ]
    }

    outcomes = score_episode(episode)

    assert [(outcome.plugin, outcome.label, outcome.metrics) for outcome in outcomes] == [
        ("human", "human-rejected", {"rejected": 1}),
        ("verifier", "verifier-failed", {"score_percent": 25}),
    ]
    assert [outcome.evidence for outcome in outcomes] == [("ev_label",), ("ev_score",)]


def test_scoring_ignores_untrusted_generic_outcome_assertions(tmp_path):
    trace = tmp_path / "episode.jsonl"
    trace.write_text(
        "\n".join(
            [
                '{"type":"outcome_label","label":"accepted"}',
                '{"type":"verifier_score","name":"quality","score":1.0}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    episode = ingest_jsonl_trace_as_episode(trace)

    outcomes = score_episode(episode)

    assert outcomes == ()


def test_canonical_user_correction_content_events_count_recurrence(tmp_path):
    trace = tmp_path / "episode.jsonl"
    trace.write_text(
        "\n".join(
            [
                '{"type":"user_correction","content":"Run tests before final."}',
                '{"type":"user_correction","content":"Please run the tests before final response."}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    episode = ingest_jsonl_trace_as_episode(trace)

    outcomes = score_episode(episode)

    assert [(outcome.plugin, outcome.label, outcome.metrics) for outcome in outcomes] == [
        ("user-correction", "recurring-user-correction", {"recurrence_count": 2})
    ]
    assert all(outcome.evidence[0].startswith("ev_") for outcome in outcomes)


def test_score_episode_accepts_explicit_plugins_without_running_defaults():
    def custom_plugin(events):
        assert len(events) == 1
        return (
            ScoreOutcome(
                plugin="custom",
                label="custom-outcome",
                metrics={"events": len(events)},
                evidence=("evt-custom",),
            ),
        )

    outcomes = score_episode(
        {"events": [{"id": "evt-test", "type": "test_result", "passed": False}]},
        plugins=(custom_plugin,),
    )

    assert outcomes == (
        ScoreOutcome(
            plugin="custom",
            label="custom-outcome",
            metrics={"events": 1},
            evidence=("evt-custom",),
        ),
    )
