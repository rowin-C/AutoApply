from autoapply.sources.ats_map import detect_ats, known_external


def test_detect_ats_known_platforms():
    assert detect_ats("https://jobs.lever.co/acme/xyz") == "lever"
    assert detect_ats("https://boards.greenhouse.io/acme/jobs/1") == "greenhouse"
    assert detect_ats("https://acme.wd3.myworkdayjobs.com/Job") == "workday"
    assert detect_ats("https://careers.smartrecruiters.com/Acme") == "smartrecruiters"


def test_detect_ats_unknown():
    assert detect_ats("https://acme.example.com/careers") is None


def test_known_external():
    assert known_external("https://lever.co/x") is True
    assert known_external("https://trials.php") is False
