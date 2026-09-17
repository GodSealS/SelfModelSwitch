"""Export normative schemas without importing the production application."""
import argparse
import inspect
import json
from pathlib import Path

import contracts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    schemas = {
        name: value.model_json_schema()
        for name, value in vars(contracts).items()
        if inspect.isclass(value) and issubclass(value, contracts.StrictModel) and value is not contracts.StrictModel
    }
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(schemas, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


if __name__ == "__main__":
    main()
