"""Optional local schema check using the existing PyYAML/jsonschema environment."""
import tempfile
from pathlib import Path

import jsonschema
import yaml

from test_pr_thread_semantics import ThreadFixture


def main():
    document = yaml.safe_load((Path(__file__).parents[1] / "openapi/product-v1.yaml").read_text(encoding="utf-8"))
    schema = {"$ref": "#/components/schemas/Item", "components": document["components"]}
    validator = jsonschema.Draft202012Validator(schema)
    with tempfile.TemporaryDirectory() as folder:
        f = ThreadFixture(Path(folder) / "contract.db")
        f.source("1", "Change")
        f.source("2", "Done", reply="1")
        f.publish("1", "change_request")
        f.publish("2", "completion_claim", dependencies=("1",))
        validator.validate(f.items()[0])
    print("Real thread Item DTO conforms to product-v1 OpenAPI schema")


if __name__ == "__main__":
    main()
