"""Bounded receipt history with cumulative counts for a single run."""
from collections import deque


class ReceiptWindow:
    def __init__(self, capacity=2048):
        self.capacity = capacity
        self.seen = set()
        self.order = deque()
        self.retired = 0
        self.total = 0

    def add(self, value):
        if type(value) is not int or value <= self.retired or value in self.seen: return
        self.seen.add(value); self.order.append(value); self.total += 1
        while len(self.order) > self.capacity:
            old = self.order.popleft(); self.seen.remove(old)
            self.retired = max(self.retired, old)

    def __len__(self): return self.total
