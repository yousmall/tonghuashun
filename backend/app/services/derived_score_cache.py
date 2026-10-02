"""Account-scoped memo of deterministic derivations, preserving computation time."""
from collections import OrderedDict
import hashlib
import json

from backend.app.fact_taxonomy import fact_is_current
from backend.app.services.model_telemetry import analysis_telemetry


class DerivedScoreCache:
    def __init__(self, max_entries=256):
        self.max_entries = max_entries
        self.entries = OrderedDict()

    def derive(self, facts, now, calculate):
        collector = analysis_telemetry.get()
        scope = collector.get("cache_scope") if collector else None
        if scope is None:
            return calculate(facts, now=now)
        output = calculate(facts, now=now)
        by_id = {f.fact_id: f for f in facts}
        results = []
        for score in output:
            # Calculate first to find the actual inputs. Unused old records do
            # not change computation time; all original records still reach
            # the verifier and its conflict checks.
            parents = [by_id[parent].model_dump(mode="json") for parent in sorted(score.derived_from)]
            key = hashlib.sha256(json.dumps([scope, score.entity, score.field, score.value,
                score.derivation_rule, parents], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            cached = self.entries.get(key)
            if cached and fact_is_current(cached, now):
                self.entries.move_to_end(key)
                results.append(cached.model_copy(deep=True))
            else:
                self.entries[key] = score.model_copy(deep=True)
                results.append(score)
            while len(self.entries) > self.max_entries:
                self.entries.popitem(last=False)
        return results
