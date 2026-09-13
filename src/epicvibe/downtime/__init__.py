"""Downtime (offline) ordering prototype.

Scenario: the EHR is down. Clinicians capture orders here from an ambient
transcript; signed orders queue durably and are written back to Epic as HL7v2
ORM^O01 messages over MLLP once the EHR returns.
"""

from epicvibe.downtime.config import DowntimeSettings

__all__ = ["DowntimeSettings"]
