"""Expose headless commands with stable contract and usage exit codes."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import mlflow
import pandas as pd

from .io import join_uri, read_csv
from .predict import predict
from .schema import ContractViolation, validate
from .train import train


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m firmaware")
    commands = parser.add_subparsers(dest="command", required=True)

    validate_parser = commands.add_parser("validate")
    validate_parser.add_argument("--input")
    validate_parser.add_argument(
        "--mode", required=True, choices=("training", "scoring")
    )

    train_parser = commands.add_parser("train")
    train_parser.add_argument("--input")
    train_parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    train_parser.add_argument(
        "--artifacts-dir"
    )

    predict_parser = commands.add_parser("predict")
    predict_parser.add_argument("--input")
    predict_parser.add_argument(
        "--artifacts-dir"
    )
    predict_parser.add_argument(
        "--output"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        data_uri = os.getenv("FIRMAWARE_DATA_URI", "data")
        artifacts_uri = os.getenv("FIRMAWARE_ARTIFACTS_URI", "artifacts")
        scores_uri = os.getenv(
            "FIRMAWARE_SCORES_URI", str(Path("outputs") / "scores.csv")
        )
        if args.command == "validate":
            default_name = (
                "deployment_events.csv"
                if args.mode == "training"
                else "upcoming_deployments.csv"
            )
            input_uri = args.input or join_uri(data_uri, default_name)
            validate(read_csv(input_uri), mode=args.mode)
            print(f"[validate] valid {args.mode} input: {input_uri}")
        elif args.command == "train":
            input_uri = args.input or join_uri(
                data_uri, "deployment_events.csv"
            )
            train(
                input_uri,
                config_path=args.config,
                artifacts_dir=args.artifacts_dir or artifacts_uri,
            )
        elif args.command == "predict":
            input_uri = args.input or join_uri(
                data_uri, "upcoming_deployments.csv"
            )
            predict(
                input_uri,
                artifacts_dir=args.artifacts_dir or artifacts_uri,
                output_path=args.output or scores_uri,
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
