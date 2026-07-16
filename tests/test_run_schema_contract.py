import unittest

from fastapi import FastAPI

from backend.api.routes.runs import router


class RunSchemaContractTests(unittest.TestCase):
    def test_run_routes_publish_skill_pin_fields_in_openapi(self):
        app = FastAPI()
        app.include_router(router)
        schema = app.openapi()

        run_schema = schema["components"]["schemas"]["RunResponse"]
        self.assertTrue(
            {
                "skill_name",
                "skill_version",
                "skill_content_hash",
                "skill_activation_source",
            }.issubset(run_schema["properties"])
        )
        self.assertEqual(
            "#/components/schemas/RunResponse",
            schema["paths"]["/v1/runs/{run_id}"]["get"]["responses"]["200"]["content"][
                "application/json"
            ]["schema"]["$ref"],
        )
        self.assertEqual(
            "#/components/schemas/RunResponse",
            schema["paths"]["/v1/runs/{run_id}/cancel"]["post"]["responses"]["200"][
                "content"
            ]["application/json"]["schema"]["$ref"],
        )
        self.assertEqual(
            "#/components/schemas/RunCreateResponse",
            schema["paths"]["/v1/threads/{thread_id}/runs"]["post"]["responses"]["201"][
                "content"
            ]["application/json"]["schema"]["$ref"],
        )
        self.assertEqual(
            "#/components/schemas/RunResumeResponse",
            schema["paths"]["/v1/runs/{run_id}/resume"]["post"]["responses"]["202"][
                "content"
            ]["application/json"]["schema"]["$ref"],
        )


if __name__ == "__main__":
    unittest.main()
