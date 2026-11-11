import pytest

from argus import privacy


@pytest.mark.parametrize("title,private", [
    ("Enter OTP - HDFC Bank", True),
    ("OTP:482913 is your code", True),
    ("Verify your CVV", True),
    ("Passwords - Settings", True),
    ("ResetPassword.tsx - atlas - Visual Studio Code", True),
    ("InPrivate - Microsoft Edge", True),
    ("Net Banking | SBI", True),
    # used to be dropped as private by substring accident
    ("Carbon Footprint report.xlsx - Excel", False),
    ("Best hotpot in Noida - Google Chrome", False),
    ("main.py - argus - Visual Studio Code", False),
])
def test_private_titles(title, private):
    assert privacy.is_private_title(title) is private


def test_clean_title_redacts_private_apps():
    assert privacy.clean_title("KeePassXC.exe", "KeePassXC", "Database.kdbx") == ("<private>", True)


@pytest.mark.parametrize("raw,masked", [
    ("key sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123", "<anthropic-key>"),
    ("token ghp_abcdefghijklmnopqrstuvwxyz0123456789", "<github-token>"),
    ("AKIAABCDEFGHIJKLMNOP in config", "<aws-key>"),
    ("password=hunter22 in .env", "password=<redacted>"),
])
def test_secrets_are_scrubbed(raw, masked):
    assert masked in privacy.scrub(raw)


def test_urls_lose_query_strings_and_sensitive_paths():
    assert privacy.trim_url("https://example.com/a/b?token=x#frag") == "https://example.com/a/b"
    assert privacy.trim_url("https://netbanking.hdfcbank.com/acct/123") == \
        "https://netbanking.hdfcbank.com/<private>"


def test_mask_email_keeps_domain():
    assert privacy.mask_email("mail sam.rivera@example.com now") == "mail s***@example.com now"
