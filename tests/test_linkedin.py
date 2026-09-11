from autoapply.settings import SearchFilter
from autoapply.sources.linkedin import build_search_url, extract_job_id


def test_base_url_and_args():
    url = build_search_url("Python Backend", "Bangalore", SearchFilter())
    assert "keywords=Python+Backend" in url
    assert "location=Bangalore" in url
    assert "start=0" in url


def test_page_offset():
    url = build_search_url("Python", "Remote", SearchFilter(), page=2)
    assert "start=50" in url


def test_posted_filter_maps():
    assert "f_TPR=r604800" in build_search_url("x", "y", SearchFilter(posted_days=7))
    assert "f_TPR=r2592000" in build_search_url("x", "y", SearchFilter(posted_days=30))
    assert "f_TPR=r86400" in build_search_url("x", "y", SearchFilter(posted_days=1))
    assert "f_TPR" not in build_search_url("x", "y", SearchFilter())


def test_remote_and_experience():
    url = build_search_url("x", "y", SearchFilter(remote_only=True, experience="mid"))
    assert "f_WT=2" in url and "f_E=3" in url


def test_job_id_extraction():
    assert extract_job_id("https://www.linkedin.com/jobs/view/12345") == "12345"
    assert (
        extract_job_id("https://www.linkedin.com/jobs/view/12345-title-slug") == "12345"
    )
    assert extract_job_id("https://example.com") is None
