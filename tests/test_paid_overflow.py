import unittest
from claude_swap.paid_overflow import choose

class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = {'accountEmail': 'overflow@example.com', 'maxMonthlyUsd': 100}
        self.emails = {'1': 'overflow@example.com', '2': 'other@example.com', '3': 'third@example.com'}
        self.usage = {'1': {'spend': {'enabled': True, 'used': 0, 'limit': 100, 'currency': 'USD'}}}
        self.headroom = {'1': 0, '2': 0, '3': 0}

    def decide(self, current='2', eligible=None):
        return choose(self.config, current, eligible or list(self.emails), self.emails, self.usage, self.headroom)

    def test_paid_fallback(self):
        self.assertEqual(self.decide(), ['1'])

    def test_stays_paid_without_flapping(self):
        self.assertEqual(self.decide('1'), [])

    def test_returns_to_included(self):
        self.headroom['3'] = 100
        self.assertEqual(self.decide('1'), ['3'])

    def test_last_included_percent_before_paid(self):
        self.headroom['3'] = 0.1
        self.assertEqual(self.decide(), ['3'])

    def test_no_premature_overflow(self):
        self.headroom['2'] = 1
        self.assertIsNone(self.decide())

    def test_unknown_account_prevents_paid(self):
        self.headroom['3'] = None
        self.assertIsNone(self.decide())

    def test_disabled_or_quarantined_target(self):
        self.assertIsNone(self.decide(eligible=['2','3']))

    def test_missing_paid_info(self):
        self.usage = {}
        self.assertIsNone(self.decide())

    def test_out_of_credits(self):
        self.usage['1']['spend']['enabled'] = False
        self.assertIsNone(self.decide())

    def test_exhausted_cap(self):
        self.usage['1']['spend']['used'] = 100
        self.assertIsNone(self.decide())

    def test_unlimited_or_increased_cap(self):
        for limit in (None, 200, float('nan')):
            self.usage['1']['spend']['limit'] = limit
            self.assertIsNone(self.decide())

    def test_different_currency(self):
        self.usage['1']['spend']['currency'] = 'EUR'
        self.assertIsNone(self.decide())

if __name__ == '__main__':
    unittest.main()
