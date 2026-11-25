using Microsoft.AspNetCore.Mvc;

namespace DbRag.Client;

// appsettings.json:  "DbRag": { "BaseUrl": "https://dbrag.internal", "ApiKey": "<from secret store>" }
[ApiController, Route("api/ask")]
public sealed class AskController(IDbRagClient rag) : ControllerBase
{
    [HttpPost]
    public async Task<IActionResult> Ask([FromBody] AskDto dto, CancellationToken ct)
    {
        var r = await rag.AskAsync(new QueryRequest(dto.Question, Summarize: dto.Summarize), ct);

        return r.Status switch
        {
            "ok"               => Ok(new { r.Answer, r.Columns, r.Rows, r.Sql, r.Truncated }),
            // question needs tables that don't exist: show the proposal to a DBA/admin - it is NEVER auto-created
            "needs_new_tables" => Ok(new { needsNewTables = true, r.MissingTables, proposedSql = r.Sql, r.Notes }),
            _                  => UnprocessableEntity(new { r.Error, r.RequestId })
        };
    }
}

public sealed record AskDto(string Question, bool Summarize = false);
