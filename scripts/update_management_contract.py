"""Synchronize the management extension with the shipped OpenAPI contract."""

import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
path = root / "src/mc_translate/contracts/openapi.json"
document = json.loads(path.read_text(encoding="utf-8"))
document["info"]["version"] = "1.4.0"
document["components"]["securitySchemes"]["AdminToken"] = {
    "type": "http",
    "scheme": "bearer",
    "description": "Private server administrator token. Never distribute to clients.",
}
schemas = document["components"]["schemas"]
summary = schemas["ProjectSummary"]
summary["properties"]["field_states"] = {
    "type": "object",
    "required": ["summary", "body"],
    "properties": {
        name: {
            "type": "string",
            "enum": [
                "ready",
                "missing",
                "queued",
                "running",
                "deferred",
                "failed",
                "blocked",
            ],
        }
        for name in ("summary", "body")
    },
    "additionalProperties": False,
}
summary["description"] = (
    "translation_state aggregates summary and body; ready requires both to match current source, language, context and policy. field_states contains individual readiness. localized_summary alone does not imply body readiness."
)
string = lambda maximum: {"type": "string", "minLength": 1, "maxLength": maximum}
schemas["ClientApplication"] = {
    "type": "object",
    "required": ["name", "contact", "purpose"],
    "properties": {
        "name": string(100),
        "contact": string(200),
        "website": {"type": "string", "maxLength": 500, "description": "Optional project homepage; HTTP URLs and other public homepage formats are accepted."},
        "purpose": string(1000),
    },
    "additionalProperties": False,
}
settings = {
    "base_url": dict(string(2048), format="uri", pattern="^https://"),
    "model": string(128),
    "reasoning_effort": {
        "type": "string",
        "enum": ["none", "minimal", "low", "medium", "high", "xhigh"],
    },
    "api_key": {"type": "string", "maxLength": 4096, "writeOnly": True},
    "daily_token_limit": {"type": "integer", "minimum": 0, "maximum": 10**10},
    "monthly_token_limit": {"type": "integer", "minimum": 0, "maximum": 10**11},
    "daily_call_limit": {"type": "integer", "minimum": 0, "maximum": 10**7},
    "model_prices": {"type": "object", "description":"USD per one million tokens, keyed by exact upstream model id. Cached input is priced separately. Prices are snapshotted per call; omitting a model price makes its cost unknown.","additionalProperties": {"type": "object", "required": ["input", "cached_input", "output"], "properties": {"input": {"type": "number", "minimum": 0,"maximum":1000000}, "cached_input": {"type": "number", "minimum": 0,"maximum":1000000}, "output": {"type": "number", "minimum": 0,"maximum":1000000}}, "additionalProperties": False}},
    "max_completion_tokens": {"type": "integer", "minimum": 256, "maximum": 131072},
    "model_timeout": {"type": "integer", "minimum": 10, "maximum": 600},
    "mail_enabled": {"type": "boolean"},
    "smtp_host": string(253),
    "smtp_port": {"type": "integer", "minimum": 1, "maximum": 65535},
    "smtp_security": {"type": "string", "enum": ["ssl", "starttls"]},
    "smtp_username": {"type": "string", "maxLength": 1024},
    "smtp_password": {"type": "string", "maxLength": 1024, "writeOnly": True},
    "mail_from": {"type": "string", "maxLength": 254},
    "mail_to": {"type": "string", "maxLength": 254},
    "mail_timeout": {"type": "integer", "minimum": 3, "maximum": 60},
}
schemas["RuntimeSettingsPatch"] = {
    "type": "object",
    "minProperties": 1,
    "properties": settings,
    "additionalProperties": False,
}
schemas["RuntimeSettings"] = {
    "type": "object",
    "properties": {
        **{k: v for k, v in settings.items() if k not in ("api_key", "smtp_password")},
        "api_key_configured": {"type": "boolean"},
        "smtp_password_configured": {"type": "boolean"},
    },
    "additionalProperties": False,
}
schemas["ClientReview"] = {
    "type": "object",
    "required": ["id", "state"],
    "properties": {
        "id": string(128),
        "state": {"type": "string", "enum": ["active", "rejected", "revoked"]},
        "fields_per_hour": {"type": "integer", "minimum": 1, "maximum": 100000},
        "requests_per_minute": {"type": "integer", "minimum": 1, "maximum": 10000},
    },
    "additionalProperties": False,
}
schemas["ClientRecord"] = {
    "type": "object",
    "properties": {
        "id": string(128),
        "prefix": string(20),
        "name": string(100),
        "contact": string(200),
        "website": string(500),
        "purpose": string(1000),
        "state": {
            "type": "string",
            "enum": ["pending", "active", "rejected", "revoked"],
        },
        "fields_per_hour": {"type": "integer"},
        "requests_per_minute": {"type": "integer"},
        "created": {"type": "number"},
        "updated": {"type": "number"},
        "fields_used_this_hour": {"type": "integer"},
        "notice": {"type": "string", "maxLength": 300},
    },
    "additionalProperties": False,
}
schemas["ClientReceipt"] = {
    "type": "object",
    "required": ["application_id", "access_token", "state", "notice"],
    "properties": {
        "application_id": string(128),
        "access_token": dict(
            string(256),
            description="Shown once. Store safely. Pending credentials cannot generate; active after approval.",
        ),
        "state": {"const": "pending"},
        "notice": string(300),
    },
    "additionalProperties": False,
}
schemas["ClientList"] = {
    "type": "object",
    "required": ["clients", "next_cursor"],
    "properties": {
        "clients": {
            "type": "array",
            "maxItems": 50,
            "items": {"$ref": "#/components/schemas/ClientRecord"},
        },
        "next_cursor": {"type": ["string", "null"]},
    },
    "additionalProperties": False,
}


def operation(identity, schema, response, auth=True, status="200"):
    result = {
        "operationId": identity,
        "summary": identity,
        "responses": {
            status: {
                "description": "Success",
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/" + response}
                    }
                },
            }
        },
        "security": [
            {"AdminToken" if identity.startswith("admin") else "ClientKey": []}
        ]
        if auth
        else [],
    }
    if schema:
        result["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/" + schema}
                }
            },
        }
    for code in ("400", "401", "403", "429"):
        result["responses"][code] = {
            "description": {
                "400": "Invalid request",
                "401": "Missing/invalid credential",
                "403": "Key awaiting approval or revoked",
                "429": "Rate limit; obey Retry-After",
            }[code]
        }
    return result


document["paths"]["/v1/admin/settings"] = {
    "get": operation("adminGetSettings", None, "RuntimeSettings"),
    "post": operation("adminUpdateSettings", "RuntimeSettingsPatch", "RuntimeSettings"),
}
schemas["ModelList"] = {
    "type": "object", "required": ["models"],
    "properties": {"models": {"type": "array", "minItems": 1, "maxItems": 1000, "items": string(128)}},
    "additionalProperties": False,
}
schemas["ModelListRequest"] = {
    "type": "object", "properties": {
        "base_url": dict(string(2048), format="uri", pattern="^https://"),
        "api_key": {"type": "string", "maxLength": 4096, "writeOnly": True},
    }, "additionalProperties": False,
}
document["paths"]["/v1/admin/models"] = {
    "get": operation("adminListModels", None, "ModelList"),
    "post": operation("adminPreviewModels", "ModelListRequest", "ModelList"),
}
schemas["MailTestRequest"] = {
    "type": "object",
    "required": ["action"],
    "properties": {"action": {"const": "test"}},
    "additionalProperties": False,
}
schemas["MailReceipt"] = {
    "type": "object",
    "required": ["id", "state"],
    "properties": {"id": string(128), "state": {"const": "pending"}},
    "additionalProperties": False,
}
schemas["MailMessage"] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "id": string(128),
        "state": {"enum": ["pending", "sending", "sent", "failed"]},
        "attempt": {"type": "integer", "minimum": 0},
        "created": {"type": "number"},
        "updated": {"type": "number"},
        "sent_at": {"type": ["number", "null"]},
        "error": {"type": ["string", "null"]},
    },
}
schemas["MailStatus"] = {
    "type": "object",
    "required": ["enabled", "counts", "messages"],
    "properties": {
        "enabled": {"type": "boolean"},
        "counts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "state": {"type": "string"},
                    "count": {"type": "integer"},
                },
                "additionalProperties": False,
            },
        },
        "messages": {
            "type": "array",
            "maxItems": 20,
            "items": {"$ref": "#/components/schemas/MailMessage"},
        },
    },
    "additionalProperties": False,
}
document["paths"]["/v1/admin/mail"] = {
    "get": operation("adminGetMailStatus", None, "MailStatus"),
    "post": operation(
        "adminQueueMailTest", "MailTestRequest", "MailReceipt", status="202"
    ),
}
schemas["AdminTaskRecord"] = {
    "type":"object", "required":["id","key","field","language","state","updated"],
    "properties":{"id":string(128),"key":string(512),"title":string(512),"field":string(64),"language":string(32),"state":string(32),"attempt":{"type":"integer"},"completed":{"type":"integer"},"total":{"type":["integer","null"]},"created":{"type":"number"},"updated":{"type":"number"},"next_attempt":{"type":"number"},"error":{"type":["object","null"]}}, "additionalProperties":True
}
schemas["AdminTaskList"]={"type":"object","required":["tasks","next_cursor","pause"],"properties":{"tasks":{"type":"array","maxItems":100,"items":{"$ref":"#/components/schemas/AdminTaskRecord"}},"next_cursor":{"type":["string","null"]},"pause":{"type":["object","null"],"properties":{"resume_at":{"type":"number"},"reason":{"enum":["daily_budget","monthly_budget"]}},"additionalProperties":False}},"additionalProperties":False}
schemas["AdminUsageRecord"]={"type":"object","properties":{"id":string(128),"model":{"type":"string"},"status":{"enum":["pending","unknown","succeeded","failed"]},"tokens":{"type":"integer"},"actual":{"type":"integer"},"input_tokens":{"type":"integer"},"cached_tokens":{"type":"integer"},"output_tokens":{"type":"integer"},"details_known":{"type":"integer"},"cost":{"type":["number","null"]},"currency":{"const":"USD"},"created":{"type":"number"}},"additionalProperties":False}
schemas["AdminUsageList"]={"type":"object","required":["period","key","calls","next_cursor","summary","currency"],"properties":{"period":{"enum":["day","month"]},"key":string(16),"calls":{"type":"array","maxItems":100,"items":{"$ref":"#/components/schemas/AdminUsageRecord"}},"next_cursor":{"type":["string","null"]},"summary":{"type":"object","additionalProperties":True},"currency":{"const":"USD"}},"additionalProperties":False}
document["paths"]["/v1/admin/tasks"]={"get":dict(operation("adminListTranslationTasks",None,"AdminTaskList"),parameters=[{"name":"state","in":"query","schema":{"type":"string"}},{"name":"cursor","in":"query","schema":{"type":"string"}}])}
document["paths"]["/v1/admin/usage"]={"get":dict(operation("adminListModelUsage",None,"AdminUsageList"),parameters=[{"name":"period","in":"query","schema":{"enum":["day","month"]}},{"name":"cursor","in":"query","schema":{"type":"string"}}])}
schemas["AdminSession"]={"type":"object","required":["authenticated"],"properties":{"authenticated":{"type":"boolean"}},"additionalProperties":False}
document["paths"]["/v1/admin/session"]={"post":operation("adminCreateBrowserSession",None,"AdminSession"),"delete":operation("adminDeleteBrowserSession",None,"AdminSession")}
document["paths"]["/v1/admin/mail"]["post"]["responses"]["409"] = {
    "description": "Mail notifications are disabled"
}
document["paths"]["/v1/admin/clients"] = {
    "get": dict(
        operation("adminListClients", None, "ClientList"),
        parameters=[
            {
                "name": "after",
                "in": "query",
                "schema": {"type": "string"},
                "description": "Cursor from the previous response, 50 records per page.",
            }
        ],
    ),
    "post": operation("adminReviewClient", "ClientReview", "ClientList"),
}
document["paths"]["/v1/clients/applications"] = {
    "post": operation(
        "applyForClientKey", "ClientApplication", "ClientReceipt", False, "201"
    )
}
document["paths"]["/v1/clients/application"] = {
    "get": operation("getClientApplicationStatus", None, "ClientRecord")
}
document["components"]["securitySchemes"]["ClientKey"] = {
    "type": "http",
    "scheme": "bearer",
    "description": "Approved developer key prefixed bct_. Reuse for ensure/jobs/events/feedback. Public reads remain anonymous. Never embed a privileged server key in distributed launchers; use your backend or individual user credentials.",
}
document["components"]["securitySchemes"]["AdminSessionCookie"] = {
    "type":"apiKey", "in":"cookie", "name":"translation_admin", "description":"Short-lived HttpOnly browser session established after administrator-token login."
}
for route, methods in document["paths"].items():
    for method, definition in methods.items():
        if method not in ("get", "post", "delete"):
            continue
        if route.startswith("/v1/admin/"):
            definition["security"] = [{"AdminSessionCookie": []}] if route == "/v1/admin/session" and method == "delete" else [{"AdminToken": []}]
        elif {"BearerAuth": []} in definition.get("security", []):
            if {"ClientKey": []} not in definition["security"]:
                definition["security"].append({"ClientKey": []})
path.write_text(
    json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
