import random
from datetime import datetime


class ColorGuessingGame:
    def play(self, group_a_total: float, group_b_total: float) -> dict:
        winner = self._bias(group_a_total, group_b_total)
        return {
            "WINNER": winner,
            "COLOR": "RED" if winner == "A" else "BLUE",
            "TIME": datetime.now().strftime("%H:%M:%S"),
        }

    @staticmethod
    def _bias(group_a_total: float, group_b_total: float) -> str:
        if group_a_total == group_b_total:
            return random.choice(("A", "B"))
        return "A" if group_a_total < group_b_total else "B"
