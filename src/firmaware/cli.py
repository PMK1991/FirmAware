"""Expose headless commands with stable contract and usage exit codes."""

from __future__ import annotations

import argparse
from pathlib import Path

import mlflow
import pandas as pd

from .predict import predict
from .schema import ContractViolation, validate
from .train import train


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m firmaware")
    commands = parser.add_subparsers(dest="command", required=True)

    validate_parser = commands.add_parser("validate")
    validate_parser.add_argument("--input", required=True, type=Path)
    validate_parser.add_argument(
        "--mode", required=True, choices=("training", "scoring")
    )

    train_parser = commands.add_parser("train")
    train_parser.add_argument("--input", required=True, type=Path)
    train_parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    train_parser.add_argument(
        "--artifacts-dir", type=Path, default=Path("artifacts")
    )

    predict_parser = commands.add_parser("predict")
    predict_parser.add_argument("--input", required=True, type=Path)
    predict_parser.add_argument(
        "--artifacts-dir", type=Path, default=Path("artifacts")
    )
    predict_parser.add_argument(
        "--output", type=Path, default=Path("outputs") / "scores.csv"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            validate(pd.read_csv(args.input), mode=args.mode)
            print(f"[validate] valid {args.mode} input: {args.input}")
        elif args.command == "train":
            train(
                args.input,
                config_path=args.config,
                artifacts_dir=args.artifacts_dir,
            )
        elif args.command == "predict":
            predict(
                args.input,
                artifacts_dir=args.artifacts_dir,
                output_path=args.output,
            )
    except (
        ContractViolation,
        mlflow.exceptions.MlflowException,
        OSError,
        pd.errors.ParserError,
        ValueError,
    ) as error:
        print(f"[error] {error}")
        return 1
    return 0
