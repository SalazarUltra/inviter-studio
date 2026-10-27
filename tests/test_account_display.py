"""Account headline helpers (name + username + phone)."""

import unittest

from core.account_display import (
    account_headline,
    account_menu_label,
    account_owner_tag,
    account_person_name,
    profile_from_user,
)


class FakeUser:
    def __init__(self, first_name=None, last_name=None, username=None):
        self.first_name = first_name
        self.last_name = last_name
        self.username = username


class AccountDisplayTests(unittest.TestCase):
    def test_profile_and_headline(self):
        profile = profile_from_user(FakeUser("Анна", "Иванова", "anna_i"))
        self.assertEqual(profile["tg_first_name"], "Анна")
        self.assertEqual(profile["tg_username"], "anna_i")
        account = {"phone": "+79990001122", **profile}
        self.assertEqual(account_person_name(account), "Анна Иванова")
        self.assertEqual(account_headline(account), "Анна Иванова · @anna_i · +79990001122")
        self.assertEqual(account_menu_label(account, used=2, limit=40), "Анна Иванова (+79990001122) (2/40)")
        self.assertEqual(account_owner_tag(account), "Анна Иванова (@anna_i)")

    def test_phone_only_when_name_unknown(self):
        account = {"phone": "+100", "tg_first_name": "", "tg_last_name": None, "tg_username": ""}
        self.assertEqual(account_headline(account), "+100")
        self.assertEqual(account_menu_label(account), "+100")
        self.assertEqual(account_owner_tag(account), "+100")

    def test_headline_omits_missing_parts(self):
        username_only = {"phone": "+905398066084", "tg_username": "ultra"}
        self.assertEqual(account_headline(username_only), "@ultra · +905398066084")
        name_only = {"phone": "+905398066084", "tg_first_name": "Alex", "tg_last_name": ""}
        self.assertEqual(account_headline(name_only), "Alex · +905398066084")
        # Warm-up / Parsing menus append a status suffix to the same headline.
        status = "Never warmed up"
        self.assertEqual(
            f"{account_headline({**username_only, 'tg_first_name': 'Alex'})} · {status}",
            "Alex · @ultra · +905398066084 · Never warmed up",
        )


if __name__ == "__main__":
    unittest.main()
