from __future__ import annotations

import json
import math

from mailbox_oncall import build_parser, run_oncall


def main() -> None:
    parser = build_parser()
    parser.description = "Watch-first mailbox oncall server entrypoint"
    parser.set_defaults(watch=True)
    parser.add_argument(
        "--idle-exit-after-seconds",
        type=float,
        help="while watching, stop after this many seconds with no claimable deliveries",
    )
    args = parser.parse_args()

    if args.idle_exit_after_seconds is not None:
        if args.idle_exit_after_seconds <= 0:
            raise ValueError("idle-exit-after-seconds must be greater than zero")
        if args.max_empty_polls is None:
            args.max_empty_polls = derive_max_empty_polls(
                idle_exit_after_seconds=float(args.idle_exit_after_seconds),
                poll_interval_seconds=float(args.poll_interval_seconds),
            )

    result = run_oncall(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def derive_max_empty_polls(
    *,
    idle_exit_after_seconds: float | None = None,
    poll_interval_seconds: float,
) -> int:
    idle_seconds = idle_exit_after_seconds
    if idle_seconds is None or idle_seconds <= 0:
        raise ValueError("idle_exit_after_seconds must be greater than zero")
    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be greater than zero")
    return max(1, int(math.ceil(idle_seconds / poll_interval_seconds)))


if __name__ == "__main__":
    main()
