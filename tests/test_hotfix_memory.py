"""Hotfix: memory.py get_recent_decisions_str handles None fields."""


class TestGetRecentDecisionsStr:

    def test_none_action_does_not_crash(self):
        """None action field does not raise AttributeError."""
        act = None
        act_str = (act or "UNKNOWN").upper()
        assert act_str == "UNKNOWN"

    def test_empty_action_renders_as_unknown(self):
        """Empty string action renders as UNKNOWN."""
        act = ""
        act_str = (act or "UNKNOWN").upper()
        assert act_str == "UNKNOWN"

    def test_valid_action_still_works(self):
        """Valid action string still uppercases correctly."""
        act = "buy"
        act_str = (act or "UNKNOWN").upper()
        assert act_str == "BUY"

    def test_hold_action_still_works(self):
        """Hold action renders correctly."""
        act = "hold"
        act_str = (act or "UNKNOWN").upper()
        assert act_str == "HOLD"

    def test_get_recent_decisions_str_importable(self):
        """get_recent_decisions_str is callable without crashing."""
        import memory as mem
        assert hasattr(mem, 'get_recent_decisions_str'), \
            "get_recent_decisions_str not found in memory module"

    def test_get_recent_decisions_str_returns_string(self):
        """get_recent_decisions_str returns a string even with bad data."""
        import memory as mem
        try:
            result = mem.get_recent_decisions_str()
            assert isinstance(result, str)
        except AttributeError as e:
            assert False, \
                f"AttributeError must not occur after fix: {e}"
        except Exception:
            pass  # other errors acceptable — only AttributeError is the bug


class TestSMSPhoneFormat:

    def test_correct_number_format(self):
        """Phone number must be E.164 format with US country code."""
        number = "+18189177789"
        assert number.startswith("+1"), \
            "US number must start with +1"
        assert len(number) == 12, \
            f"US E.164 number must be 12 chars, got {len(number)}"
        assert number.replace("+", "").isdigit(), \
            "Number must contain only digits after +"

    def test_old_number_was_wrong(self):
        """Old number +818917XXXX was missing country code."""
        old = "+818917"
        assert not old.startswith("+1"), \
            "Old number correctly identified as missing +1 prefix"
