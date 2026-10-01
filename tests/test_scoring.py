import unittest

from app import scoring


def make_rows(count=80, daily=0.006, final_jump=0.0):
    rows = []
    price = 10.0
    for index in range(count):
        open_price = price
        price *= 1 + daily
        if index == count - 1:
            price *= 1 + final_jump
        rows.append(
            {
                "date": f"2026-01-{index + 1:02d}",
                "open": round(open_price, 4),
                "high": round(max(open_price, price) * 1.012, 4),
                "low": round(min(open_price, price) * 0.988, 4),
                "close": round(price, 4),
                "volume": 1_000_000 + index * 20_000,
                "amount": 20_000_000 + index * 100_000,
                "turnover": 3.2,
            }
        )
    return rows


class ScoringTests(unittest.TestCase):
    def test_standard_indicators_and_technical_score(self):
        rows = make_rows()
        indicators = scoring.standard_indicators(rows)
        self.assertEqual(indicators["count"], 80)
        self.assertIsNotNone(indicators["latest"]["ma20"])
        result = scoring.technical_score(rows)
        self.assertEqual(set(result["components"]), {"ma5", "ma10", "kdj", "macd"})
        self.assertGreaterEqual(result["total"], 3)
        self.assertEqual(len(scoring.trend_score_series(rows)), 10)

    def test_auction_gates_are_auditable(self):
        rows = make_rows()
        snapshot = {
            "code": "600001",
            "name": "测试股份",
            "prev_close": rows[-1]["close"],
            "auction_price": rows[-1]["close"] * 1.025,
            "market_cap": 8_000_000_000,
            "auction_amount": 80_000_000,
            "volume_ratio": 2.1,
            "turnover": 4.5,
            "amplitude": 7.2,
            "is_first_board": True,
            "board_count": 1,
            "seal_time": "10:12",
            "sector_resonance": 0.8,
        }
        gates = scoring.evaluate_auction_gates(snapshot, rows)
        self.assertEqual(len(gates), 17)
        self.assertTrue(all({"name", "pass", "reason", "value"} <= set(gate) for gate in gates))
        result = scoring.auction_score(snapshot, rows, {"score": 2, "coefficient": 1.02})
        self.assertEqual(len(result["breakdown"]), 10)
        self.assertGreaterEqual(result["score"], 0)
        self.assertLessEqual(result["score"], 100)

    def test_board_rotation_and_explicit_risk_veto(self):
        rotation = scoring.board_rotation_score([
            {"name": "测试板块", "change_pct": 2.0, "returns_10d": 12.0, "up_count": 8, "down_count": 2}
        ])
        self.assertEqual(rotation["top"][0]["state"], "观察")
        risk = scoring.evaluate_rulebook_risk({"code": "600001", "name": "测试股份", "price": 10, "major_risk": True}, make_rows())
        self.assertTrue(risk["hard_veto"])
    def test_deep_phases_do_not_overlap(self):
        result = scoring.deep_analysis(make_rows(45), max_days=30)
        self.assertEqual(result["rows_used"], 30)
        phases = result["phases"]
        self.assertTrue(phases)
        self.assertEqual(sum(item["days"] for item in phases), 30)
        self.assertIn("summary", result["conclusion"])


if __name__ == "__main__":
    unittest.main()



