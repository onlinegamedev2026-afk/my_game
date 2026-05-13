import random
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

RANKS = ("2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K", "A")
SUITS = ("H", "D", "C", "S")
RANK_VALUE = {rank: i for i, rank in enumerate(RANKS, start=2)}
Winner = Literal["A", "B", "TIE"]


@dataclass(frozen=True)
class Card:
    rank: str
    suit: str


class Deck:
    def __init__(self) -> None:
        self.cards = [Card(rank, suit) for rank in RANKS for suit in SUITS]
        random.shuffle(self.cards)

    def deal(self, count: int) -> list[Card]:
        return [self.cards.pop() for _ in range(count)]


class HandEvaluator:
    @staticmethod
    def _is_sequence(values: list[int]) -> bool:
        sorted_values = sorted(values)
        if sorted_values == [2, 3, 14]:
            return True
        return sorted_values[2] - sorted_values[1] == 1 and sorted_values[1] - sorted_values[0] == 1

    @classmethod
    def evaluate(cls, hand: list[Card]) -> tuple[int, tuple[int, ...]]:
        values = sorted([RANK_VALUE[card.rank] for card in hand])
        suits = [card.suit for card in hand]
        counts = Counter(values)
        freq = sorted(counts.values(), reverse=True)
        is_flush = len(set(suits)) == 1
        is_seq = cls._is_sequence(values)
        if freq == [3]:
            return 6, (values[2],)
        if is_seq and is_flush:
            return 5, (3 if values == [2, 3, 14] else values[2],)
        if is_seq:
            return 4, (3 if values == [2, 3, 14] else values[2],)
        if is_flush:
            return 3, tuple(sorted(values, reverse=True))
        if freq == [2, 1]:
            pair_value = next(k for k, v in counts.items() if v == 2)
            kicker = next(k for k, v in counts.items() if v == 1)
            return 2, (pair_value, kicker)
        return 1, tuple(sorted(values, reverse=True))

    @classmethod
    def compare(cls, left: list[Card], right: list[Card]) -> Winner:
        left_rating = cls.evaluate(left)
        right_rating = cls.evaluate(right)
        if left_rating > right_rating:
            return "A"
        if right_rating > left_rating:
            return "B"
        return "TIE"


class TinPattiGame:
    def play(self, group_a_total: float, group_b_total: float) -> dict[str, object]:
        target = self._bias(group_a_total, group_b_total)
        while True:
            deck = Deck()
            group_a = deck.deal(3)
            group_b = deck.deal(3)
            winner = HandEvaluator.compare(group_a, group_b)
            if winner != "TIE" and (target == "ANY" or winner == target):
                return {
                    "A": [(card.rank, card.suit) for card in group_a],
                    "B": [(card.rank, card.suit) for card in group_b],
                    "WINNER": winner,
                    "TIME": datetime.now().strftime("%H:%M:%S"),
                }

    @staticmethod
    def _bias(group_a_total: float, group_b_total: float) -> str:
        if group_a_total == group_b_total:
            return "ANY"
        return "A" if group_a_total < group_b_total else "B"
