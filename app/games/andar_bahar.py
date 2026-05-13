import random
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

RANKS = ("2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K", "A")
SUITS = ("H", "D", "C", "S")
Winner = Literal["A", "B"]


class AndarBaharError(Exception):
    pass


@dataclass(frozen=True)
class Card:
    rank: str
    suit: str


@dataclass(frozen=True)
class RoundResult:
    joker: Card
    andar: list[Card]
    bahar: list[Card]
    winner: Winner
    winning_card: Card
    deal_order: list[Winner]


class Deck:
    def __init__(self) -> None:
        self.cards = [Card(rank, suit) for rank in RANKS for suit in SUITS]
        random.shuffle(self.cards)

    def deal_one(self) -> Card:
        if not self.cards:
            raise AndarBaharError("Deck exhausted before a matching card was found.")
        return self.cards.pop()


class AndarBaharGame:
    def play(self, group_a_total: float, group_b_total: float) -> dict[str, object]:
        target = self._bias(group_a_total, group_b_total)
        while True:
            result = self._deal_round(start_side=random.choice(("A", "B")))
            if target == "ANY" or result.winner == target:
                return {
                    "JOKER": (result.joker.rank, result.joker.suit),
                    "A": [(card.rank, card.suit) for card in result.andar],
                    "B": [(card.rank, card.suit) for card in result.bahar],
                    "WINNER": result.winner,
                    "WINNING_CARD": (result.winning_card.rank, result.winning_card.suit),
                    "DEAL_ORDER": result.deal_order,
                    "TOTAL_DRAWS": len(result.deal_order),
                    "TIME": datetime.now().strftime("%H:%M:%S"),
                }

    @staticmethod
    def _bias(group_a_total: float, group_b_total: float) -> str:
        if group_a_total == group_b_total:
            return "ANY"
        return "A" if group_a_total < group_b_total else "B"

    @staticmethod
    def _deal_round(start_side: Winner) -> RoundResult:
        deck = Deck()
        joker = deck.deal_one()
        andar: list[Card] = []
        bahar: list[Card] = []
        deal_order: list[Winner] = []
        current_side = start_side

        while deck.cards:
            card = deck.deal_one()
            deal_order.append(current_side)
            if current_side == "A":
                andar.append(card)
            else:
                bahar.append(card)
            if card.rank == joker.rank:
                return RoundResult(joker, andar, bahar, current_side, card, deal_order)
            current_side = "B" if current_side == "A" else "A"

        raise AndarBaharError("No matching card found.")
