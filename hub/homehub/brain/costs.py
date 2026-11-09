from datetime import date


class CostTracker:
    def __init__(self, storage, emit=None):
        self.storage = storage
        self.emit = emit
        self.session_usd = 0.0
        self.turns = 0

    def reset_session(self):
        self.session_usd = 0.0
        self.turns = 0

    def record(self, conversation_id, person_id, model, heard, reply, steps, usd, latency=None):
        usd = float(usd or 0.0)
        turn_id = self.storage.add_turn(conversation_id, heard, reply, model, steps, usd, latency)
        self.session_usd += usd
        self.turns += 1
        day_usd = self.storage.cost_total(day=date.today())
        if self.emit is not None:
            self.emit("cost.turn", person_id=person_id, model=model, usd=round(usd, 6), session_usd=round(self.session_usd, 6),
                      day_usd=round(day_usd, 6))
        return turn_id

    def summary(self):
        return (f"${self.session_usd:.4f} this session over {self.turns} turn(s), "
                f"${self.storage.cost_total(day=date.today()):.4f} today")
