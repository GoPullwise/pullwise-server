"""Offline planning arithmetic, not billing telemetry or an enforced spend limit."""
import json
import math


def excess(usage, included):
    return max(0, usage - included)


def estimate(api_requests, stored_rows, d1_gb, do_gb, *, do_writes_per_request=80):
    # Application-only native sample: 85 retained HTTP responses, 1,078 reads,
    # 345 writes. Final preview adds refresh only after a mutating batch.
    worker_requests = api_requests * 1.2  # 20% dynamic HTML/account overhead.
    d1_reads = api_requests * (1078 / 85 + 0.3 * (stored_rows + 6))
    d1_writes = api_requests * 345 / 85
    do_reads = api_requests * 300  # Engineering allowance; not observed usage.
    do_writes = api_requests * do_writes_per_request
    costs = {
        "workers_base": 5,
        "workers_requests": excess(worker_requests, 10_000_000) / 1_000_000 * 0.30,
        "workers_cpu": excess(worker_requests * 100, 30_000_000) / 1_000_000 * 0.02,
        "d1_reads": excess(d1_reads, 25_000_000_000) / 1_000_000 * 0.001,
        "d1_writes": excess(d1_writes, 50_000_000) / 1_000_000,
        "d1_storage": excess(d1_gb, 5) * 0.75,
        "do_requests": math.ceil(excess(api_requests, 1_000_000) / 1_000_000) * 0.15,
        # One singleton even active all 30 days: 331,776 GB-s, under 400,000.
        "do_duration": 0,
        "do_reads": excess(do_reads, 25_000_000_000) / 1_000_000 * 0.001,
        "do_writes": excess(do_writes, 50_000_000) / 1_000_000,
        "do_storage": excess(do_gb, 5) * 0.20,
    }
    total = sum(costs.values())
    return {
        "api_requests": api_requests, "stored_physical_rows": stored_rows,
        "post_write_refresh_batches": api_requests * 0.3,
        "d1_rows_read": round(d1_reads), "d1_rows_written": round(d1_writes),
        "do_rows_read": do_reads, "do_rows_written": do_writes,
        "components_usd": {key: round(value, 4) for key, value in costs.items()},
        "estimated_total_usd": round(total, 2), "headroom_to_200_usd": round(200 - total, 2),
    }


if __name__ == "__main__":
    print(json.dumps({
        "pilot": estimate(600_000, 500, 0.5, 1),
        "steady": estimate(1_200_000, 5_000, 2, 5),
        "growth": estimate(2_000_000, 10_000, 10, 10),
        "growth_do_write_sensitivity": estimate(2_000_000, 10_000, 10, 10,
            do_writes_per_request=120),
    }, indent=2))
