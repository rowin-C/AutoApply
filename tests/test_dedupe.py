from autoapply.db import (
    Listing,
    ListingStatus,
    configure_engine,
    get_session,
    init_db,
    make_dedupe_key,
    register_spend,
    remaining_budget,
    spent_today,
    upsert_listing,
)


def _setup(tmp_path):
    configure_engine(f"sqlite:///{tmp_path}/test.db")
    init_db()
    return get_session()


def test_dedupe_keys():
    assert make_dedupe_key("linkedin", "123") == "linkedin:123"
    a = make_dedupe_key("linkedin", None, "Title", "Company", "Bangalore")
    b = make_dedupe_key("linkedin", None, "Title", "Company", "Bangalore")
    c = make_dedupe_key("linkedin", None, "Title", "Company", "Remote")
    assert a == b and a != c


def test_upsert_new_then_update(tmp_path):
    db = _setup(tmp_path)
    l1 = Listing(
        source="linkedin", job_id="1", url="u", dedupe_key="linkedin:1", title="A"
    )
    is_new, _ = upsert_listing(db, l1)
    assert is_new is True

    l2 = Listing(
        source="linkedin", job_id="1", url="u2", dedupe_key="linkedin:1", title="B"
    )
    is_new2, saved2 = upsert_listing(db, l2)
    assert is_new2 is False
    assert saved2.title == "B" and saved2.url == "u2"

    assert len(db.exec(__import__("sqlmodel").select(Listing)).all()) == 1


def test_daily_budget_ledger(tmp_path):
    db = _setup(tmp_path)
    assert spent_today(db, "detail_fetch") == 0
    register_spend(db, "detail_fetch", 3)
    assert spent_today(db, "detail_fetch") == 3
    assert remaining_budget(db, "detail_fetch", 5) == 2
    assert remaining_budget(db, "detail_fetch", None) is None


def test_status_value(tmp_path):
    db = _setup(tmp_path)
    l = Listing(
        source="linkedin", dedupe_key="linkedin:x", status=ListingStatus.QUEUED.value
    )
    upsert_listing(db, l)
    assert l.status == "queued"
