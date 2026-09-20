"""``remove`` renumbers the remaining accounts 1…n (Infinitus #374)."""

from unittest.mock import patch

from claude_swap.exceptions import SessionError
from claude_swap.models import Platform
from claude_swap.switcher import ClaudeAccountSwitcher


def _switcher(emails):
    s = ClaudeAccountSwitcher()
    s.platform = Platform.LINUX
    s._setup_directories()
    s._init_sequence_file()
    for i, email in enumerate(emails, start=1):
        s.add_account_from_token(f"sk-ant-oat01-{i}", email=email)
    return s


def _roster(s):
    data = s._get_sequence_data()
    return {num: acc["email"] for num, acc in data["accounts"].items()}, data


class TestRemoveCompactsSlots:
    def test_removing_the_middle_closes_the_gap(self, temp_home, capsys):
        s = _switcher(["a@x.com", "b@x.com", "c@x.com"])
        s.set_alias("3", "cee")
        s.set_account_disabled("3", True)
        capsys.readouterr()

        s.remove_account("2", assume_yes=True)

        roster, data = _roster(s)
        assert roster == {"1": "a@x.com", "2": "c@x.com"}
        assert data["sequence"] == [1, 2]
        assert data["accounts"]["2"]["alias"] == "cee"
        assert s.is_account_disabled("2") is True
        assert s._read_backup_or_abort("2", "c@x.com")  # backup followed the account
        assert "Renumbered 3→2" in capsys.readouterr().out

    def test_an_old_gap_closes_too(self, temp_home):
        s = _switcher(["a@x.com", "b@x.com", "c@x.com", "d@x.com"])
        data = s._get_sequence_data()
        data["accounts"]["5"] = data["accounts"].pop("4")
        data["sequence"] = [1, 2, 3, 5]
        s._write_json(s.sequence_file, data)
        s._write_account_credentials("5", "d@x.com", "sk-ant-oat01-4")

        s.remove_account("2", assume_yes=True)

        roster, data = _roster(s)
        assert roster == {"1": "a@x.com", "2": "c@x.com", "3": "d@x.com"}
        assert data["sequence"] == [1, 2, 3]

    def test_removing_the_last_moves_nothing(self, temp_home, capsys):
        s = _switcher(["a@x.com", "b@x.com"])
        capsys.readouterr()

        s.remove_account("2", assume_yes=True)

        assert _roster(s)[0] == {"1": "a@x.com"}
        assert "Renumbered" not in capsys.readouterr().out

    def test_removing_the_active_account_clears_active(self, temp_home):
        s = _switcher(["a@x.com", "b@x.com", "c@x.com"])
        data = s._get_sequence_data()
        data["activeAccountNumber"] = 2
        s._write_json(s.sequence_file, data)

        s.remove_account("2", assume_yes=True)

        assert s._get_sequence_data()["activeAccountNumber"] is None

    def test_active_number_follows_its_account(self, temp_home):
        s = _switcher(["a@x.com", "b@x.com", "c@x.com"])
        data = s._get_sequence_data()
        data["activeAccountNumber"] = 3
        s._write_json(s.sequence_file, data)

        s.remove_account("1", assume_yes=True)

        roster, data = _roster(s)
        assert roster == {"1": "b@x.com", "2": "c@x.com"}
        assert data["activeAccountNumber"] == 2

    def test_a_live_session_stops_the_renumber_and_says_how_to_finish(self, temp_home, capsys):
        s = _switcher(["a@x.com", "b@x.com", "c@x.com", "d@x.com"])

        def guard(num, email, action):
            if num == "3":
                raise SessionError(f"Account-{num} ({email}) has a live session.")

        with patch.object(s, "_ensure_no_live_session", side_effect=guard):
            s.remove_account("2", assume_yes=True)

        roster, data = _roster(s)
        assert roster == {"1": "a@x.com", "3": "c@x.com", "4": "d@x.com"}
        out = capsys.readouterr().out
        assert "Slots 3, 4 keep their numbers" in out
        assert "cswap move 3 2 && cswap move 4 3" in out
