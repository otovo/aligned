from fastapi import FastAPI, HTTPException
from aladdin.feature_store import FeatureStore
from aladdin.feature import Feature
from numpy import nan

class FastAPIServer:

    @staticmethod
    def model_path(name: str, feature_store: FeatureStore, app: FastAPI):
        feature_request = feature_store.model_requests[name]

        entities: set[Feature] = set()
        for request in feature_request.needed_requests:
            entities.update(request.entities)

        required_features = entities.copy()
        for request in feature_request.needed_requests:
            if isinstance(request, list):
                for sub_request in request:
                    required_features.update(sub_request.all_required_features)
            else:
                required_features.update(request.all_required_features)

        featch_api_schema = {
            "requestBody": {
                "content": {
                    "application/json": {
                        "schema": {
                            "required": [entity.name for entity in entities],
                            "type": "object",
                            "properties": {
                                entity.name: {
                                    "type": "array", 
                                    "items": { "type": entity.dtype.name }
                                } for entity in entities
                            },
                        }
                    }
                },
                "required": True,
            },
        }
        write_api_schema = {
            "requestBody": {
                "content": {
                    "application/json": {
                        "schema": {
                            "required": [feature.name for feature in required_features],
                            "type": "object",
                            "properties": {
                                feature.name: {
                                    "type": "array", 
                                    "items": { "type": feature.dtype.name }
                                } for feature in required_features
                            },
                        }
                    }
                },
                "required": True,
            },
        }

        # Using POST as this can have a body with the fact / entity table
        @app.post(f"/{name}", openapi_extra=featch_api_schema)
        async def get_model(entity_values: dict) -> dict:
            missing_entities = { entity.name for entity in entities if entity.name not in entity_values }
            if missing_entities:
                raise HTTPException(status_code=400, detail=f"Missing entity values {missing_entities}")

            df = await feature_store.model(name).features_for(entity_values).to_df()
            df.replace(nan, value=None, inplace=True)
            return df.to_dict("list")

        @app.post(f"/{name}/write", openapi_extra=write_api_schema)
        async def write_model(feature_values: dict) -> dict:
            missing_features = { entity.name for entity in required_features if entity.name not in feature_values }
            if missing_features:
                raise HTTPException(status_code=400, detail=f"Missing feature values {missing_features}")

            await feature_store.model(name).write(feature_values)

    @staticmethod
    def run(feature_store: FeatureStore, host: str | None = None, port: int | None = None, workers: int | None = None):
        from fastapi import FastAPI
        import uvicorn

        app = FastAPI()
        app.docs_url = "/docs"

        for model in feature_store.all_models:
            FastAPIServer.model_path(model, feature_store, app)
        
        uvicorn.run(
            app, 
            host=host or "127.0.0.1", 
            port=port or 8000, 
            workers=workers or workers
        )