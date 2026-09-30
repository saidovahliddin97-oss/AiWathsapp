from app.memory import Store, detect_fact_candidates, load_relatives_config, normalize_phone
from app.models import Fact


def test_phone_normalisation(store):
    assert normalize_phone("+992 (90) 000-00-01") == "992900000001"
    assert store.find_relative_by_phone("+992900000001").id == "mom"


def test_history_limit_and_order(store):
    for i in range(40):
        store.add_message("mom", "relative" if i % 2 == 0 else "assistant", f"паём {i}")
    hist = store.recent_messages("mom", limit=24)
    assert len(hist) == 24 and hist[0].text == "паём 16" and hist[-1].text == "паём 39"


def test_history_char_budget(store):
    store.add_message("mom", "relative", "а" * 5000)
    store.add_message("mom", "relative", "б" * 5000)
    hist = store.recent_messages("mom", limit=24, max_chars=6000)
    assert len(hist) == 1 and hist[0].text.startswith("б")


def test_history_isolated_per_relative(store):
    store.add_message("mom", "relative", "салом")
    assert store.recent_messages("uncle") == []


def test_event_idempotency(store):
    assert store.claim_event("e1") is True
    assert store.claim_event("e1") is False
    store.finish_event("e1", "retryable")
    assert store.claim_event("e1") is True
    store.finish_event("e1", "retryable")
    assert store.claim_event("e1") is True   # attempt 3
    store.finish_event("e1", "retryable")
    assert store.claim_event("e1") is False  # bounded


def test_fact_conflict_priority(store):
    assert store.add_fact(Fact(fact="Корбар дар Душанбе", source="conversation", confidence=0.8), key="city")
    assert store.add_fact(Fact(fact="Корбар дар Маскав", source="user"), key="city")
    facts = [f.fact for f in store.get_facts(None)]
    assert facts == ["Корбар дар Маскав"]
    # less reliable source can't override
    assert store.add_fact(Fact(fact="Корбар дар Хуҷанд", source="conversation", confidence=0.9), key="city") is False
    assert [f.fact for f in store.get_facts(None)] == ["Корбар дар Маскав"]


def test_fact_scope(store):
    store.add_fact(Fact(fact="global", source="user"))
    store.add_fact(Fact(fact="mom only", source="user", relative_id="mom"))
    assert {f.fact for f in store.get_facts("mom")} == {"global", "mom only"}
    assert {f.fact for f in store.get_facts("uncle")} == {"global"}


def test_low_confidence_excluded(store):
    store.add_fact(Fact(fact="maybe", source="conversation", confidence=0.3))
    assert store.get_facts(None) == []


def test_candidate_detection():
    assert "wedding" in detect_fact_candidates("Шанбе тӯйи Фаррух аст")
    assert "health" in detect_fact_candidates("Бобо бемор шуд")
    assert detect_fact_candidates("Салом, чӣ хел?") == []
    assert detect_fact_candidates("ок") == []


def test_candidate_reject(store):
    cid = store.add_fact_candidate("mom", "Бобо бемор шуд", "health")
    assert store.resolve_fact_candidate(cid, approve=False)
    assert store.get_facts("mom") == []
    assert store.resolve_fact_candidate(cid, approve=True) is False


def test_load_example_config(tmp_path):
    s = Store(tmp_path / "x.db")
    assert load_relatives_config(s, "config/relatives.example.json") >= 5
    assert s.find_relative_by_phone("992900000001").relation == "mother"
    assert load_relatives_config(s, tmp_path / "missing.json") == 0
