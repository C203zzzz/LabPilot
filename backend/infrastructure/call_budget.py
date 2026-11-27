"""Request-local paid call cap shared by image and text transports."""
from contextvars import ContextVar

active_budget = ContextVar('document_call_budget', default=None)
research_reservation = ContextVar('research_model_reservation', default=None)

def reserve():
    callback = research_reservation.get()
    if callback is not None:
        callback()
    budget = active_budget.get()
    if budget is None:
        return True
    if budget['used'] >= budget['limit']:
        return False
    budget['used'] += 1
    return True
