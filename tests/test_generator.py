from collections import Counter

from gform.config import Config
from gform.generator import generate


def make_cfg(mode="exact", count=1000, seed=7):
    return Config(
        form={"url": "https://x/viewform"},
        generation={"count": count, "seed": seed, "mode": mode},
        questions=[
            {
                "entry_id": "111111",
                "title": "Color",
                "type": "radio",
                "distribution": {"Red": 0.6, "Blue": 0.3, "Green": 0.1},
            },
            {
                "entry_id": "333333",
                "title": "Features",
                "type": "checkbox",
                "distribution": {"A": 0.5, "B": 0.2},
            },
        ],
    )


def test_exact_marginals_match_targets():
    responses = generate(make_cfg())
    counts = Counter(r.answers["111111"] for r in responses)
    assert counts["Red"] == 600
    assert counts["Blue"] == 300
    assert counts["Green"] == 100


def test_checkbox_inclusion_counts_exact():
    responses = generate(make_cfg())
    a = sum(1 for r in responses if "A" in r.answers["333333"])
    b = sum(1 for r in responses if "B" in r.answers["333333"])
    assert a == 500
    assert b == 200


def test_full_determinism():
    a = [r.answers for r in generate(make_cfg())]
    b = [r.answers for r in generate(make_cfg())]
    assert a == b


def test_different_seed_changes_output():
    a = [r.answers for r in generate(make_cfg(seed=1))]
    b = [r.answers for r in generate(make_cfg(seed=2))]
    assert a != b


def test_grid_rows_generate_like_radio_and_checkbox():
    cfg = Config(
        form={"url": "https://x/viewform"},
        generation={"count": 100, "seed": 3, "mode": "exact"},
        questions=[
            {
                "entry_id": "611111",
                "title": "Grid [Service]",
                "type": "grid_radio",
                "distribution": {"Bad": 0.25, "Good": 0.75},
            },
            {
                "entry_id": "711111",
                "title": "Avail [Week1]",
                "type": "grid_checkbox",
                "distribution": {"Mon": 0.6, "Tue": 0.3},
            },
        ],
    )
    responses = generate(cfg)
    radio_counts = Counter(r.answers["611111"] for r in responses)
    assert radio_counts["Good"] == 75 and radio_counts["Bad"] == 25
    mon = sum(1 for r in responses if "Mon" in r.answers["711111"])
    assert mon == 60
    # grid_radio -> single string; grid_checkbox -> list
    assert isinstance(responses[0].answers["611111"], str)
    assert isinstance(responses[0].answers["711111"], list)


def test_probabilistic_is_deterministic_per_seed():
    a = [r.answers for r in generate(make_cfg(mode="probabilistic"))]
    b = [r.answers for r in generate(make_cfg(mode="probabilistic"))]
    assert a == b


def make_text_cfg(pool, count=10, seed=7, mode="exact"):
    return Config(
        form={"url": "https://x/viewform"},
        generation={"count": count, "seed": seed, "mode": mode},
        questions=[
            {"entry_id": "555555", "title": "Name", "type": "text",
             "distribution": pool},
        ],
    )


def test_text_pool_exact_quotas():
    responses = generate(make_text_cfg({"Good": 0.7, "Bad": 0.3}))
    counts = Counter(r.answers["555555"] for r in responses)
    assert counts["Good"] == 7
    assert counts["Bad"] == 3
    assert isinstance(responses[0].answers["555555"], str)


def test_text_pool_unnormalized_weights_equal_normalized():
    a = [r.answers for r in generate(make_text_cfg({"Yes": 2.0, "No": 2.0}))]
    b = [r.answers for r in generate(make_text_cfg({"Yes": 0.5, "No": 0.5}))]
    assert a == b


def test_text_pool_deterministic_per_seed():
    pool = {"Great service": 1.0, "Could be better": 1.0, "No comment": 2.0}
    a = [r.answers for r in generate(make_text_cfg(pool, mode="probabilistic"))]
    b = [r.answers for r in generate(make_text_cfg(pool, mode="probabilistic"))]
    assert a == b


def make_persona_cfg(count=100, seed=7, mode="exact"):
    """Two opposite archetypes over a radio + a text question.

    'happy' always rates High and writes the happy comment; 'sad' is the
    mirror image. The base distributions are deliberately different so we can
    tell overrides (not the base) drove the answers.
    """
    return Config(
        form={"url": "https://x/viewform"},
        generation={"count": count, "seed": seed, "mode": mode},
        questions=[
            {"entry_id": "111111", "title": "Rating", "type": "radio",
             "distribution": {"High": 0.5, "Low": 0.5}},
            {"entry_id": "222222", "title": "Comment", "type": "text",
             "distribution": {"meh": 1.0}},
        ],
        personas=[
            {"name": "happy", "weight": 0.7, "distributions": {
                "111111": {"High": 1.0, "Low": 0.0},
                "222222": {"Loved it": 1.0},
            }},
            {"name": "sad", "weight": 0.3, "distributions": {
                "111111": {"High": 0.0, "Low": 1.0},
                "222222": {"Hated it": 1.0},
            }},
        ],
    )


def test_persona_allocation_and_correlation():
    responses = generate(make_persona_cfg(count=100, seed=7))
    assert len(responses) == 100
    high = [r for r in responses if r.answers["111111"] == "High"]
    low = [r for r in responses if r.answers["111111"] == "Low"]
    # weights 0.7 / 0.3 over 100 -> exact block sizes.
    assert len(high) == 70
    assert len(low) == 30
    # Within-respondent correlation: the rating and the free text agree.
    assert all(r.answers["222222"] == "Loved it" for r in high)
    assert all(r.answers["222222"] == "Hated it" for r in low)


def test_persona_full_determinism():
    a = [r.answers for r in generate(make_persona_cfg())]
    b = [r.answers for r in generate(make_persona_cfg())]
    assert a == b


def test_persona_falls_back_to_base_distribution():
    # 'only' overrides the radio but not the text question -> text uses the base.
    cfg = Config(
        form={"url": "https://x/viewform"},
        generation={"count": 10, "seed": 3, "mode": "exact"},
        questions=[
            {"entry_id": "111111", "title": "R", "type": "radio",
             "distribution": {"A": 0.5, "B": 0.5}},
            {"entry_id": "222222", "title": "C", "type": "text",
             "distribution": {"base-answer": 1.0}},
        ],
        personas=[
            {"name": "only", "weight": 1.0,
             "distributions": {"111111": {"A": 1.0, "B": 0.0}}},
        ],
    )
    responses = generate(cfg)
    assert all(r.answers["111111"] == "A" for r in responses)            # override
    assert all(r.answers["222222"] == "base-answer" for r in responses)  # fallback


def test_no_personas_is_unchanged():
    # An empty personas list must reproduce the independent-column path exactly.
    with_empty = Config(**{**make_cfg().model_dump(), "personas": []})
    a = [r.answers for r in generate(make_cfg())]
    b = [r.answers for r in generate(with_empty)]
    assert a == b


def test_text_column_uses_same_seed_scheme_as_choice():
    # A text question must not perturb the per-column seeds of questions
    # around it: the choice column at index 0 is identical whether the text
    # question follows it or not.
    base = make_cfg(count=50)
    with_text = Config(
        form={"url": "https://x/viewform"},
        generation={"count": 50, "seed": 7, "mode": "exact"},
        questions=base.questions
        + [
            type(base.questions[0])(
                entry_id="555555", title="Name", type="text",
                distribution={"Alice": 1.0, "Bob": 1.0},
            )
        ],
    )
    a = [r.answers["111111"] for r in generate(base)]
    b = [r.answers["111111"] for r in generate(with_text)]
    assert a == b
