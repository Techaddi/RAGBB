using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;

namespace DbRag.Client;

public sealed class DbRagOptions
{
    public string BaseUrl { get; set; } = "http://localhost:8000";
    public string ApiKey { get; set; } = "";
    public int TimeoutSeconds { get; set; } = 90;
}

public sealed record QueryRequest(
    [property: JsonPropertyName("question")] string Question,
    [property: JsonPropertyName("execute")] bool Execute = true,
    [property: JsonPropertyName("max_rows")] int? MaxRows = null,
    [property: JsonPropertyName("summarize")] bool Summarize = false);

public sealed record MissingTable(
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("purpose")] string Purpose,
    [property: JsonPropertyName("ddl")] string Ddl);

public sealed record QueryResponse(
    [property: JsonPropertyName("request_id")] string RequestId,
    [property: JsonPropertyName("question")] string Question,
    [property: JsonPropertyName("status")] string Status,            // ok | needs_new_tables | error
    [property: JsonPropertyName("sql")] string? Sql,
    [property: JsonPropertyName("tables_used")] List<string> TablesUsed,
    [property: JsonPropertyName("missing_tables")] List<MissingTable> MissingTables,
    [property: JsonPropertyName("executed")] bool Executed,
    [property: JsonPropertyName("columns")] List<string> Columns,
    [property: JsonPropertyName("rows")] List<List<JsonElement>> Rows,
    [property: JsonPropertyName("row_count")] int RowCount,
    [property: JsonPropertyName("truncated")] bool Truncated,
    [property: JsonPropertyName("answer")] string? Answer,
    [property: JsonPropertyName("notes")] string? Notes,
    [property: JsonPropertyName("warnings")] List<string> Warnings,
    [property: JsonPropertyName("cached_plan")] bool CachedPlan,
    [property: JsonPropertyName("attempts")] int Attempts,
    [property: JsonPropertyName("timings_ms")] Dictionary<string, int> TimingsMs,
    [property: JsonPropertyName("error")] string? Error);

public interface IDbRagClient
{
    Task<QueryResponse> AskAsync(QueryRequest request, CancellationToken ct = default);
    Task<QueryResponse> GenerateSqlAsync(string question, CancellationToken ct = default);
}

public sealed class DbRagClient(HttpClient http) : IDbRagClient
{
    public async Task<QueryResponse> AskAsync(QueryRequest request, CancellationToken ct = default)
        => await PostAsync("/v1/query", request, ct);

    public async Task<QueryResponse> GenerateSqlAsync(string question, CancellationToken ct = default)
        => await PostAsync("/v1/sql", new QueryRequest(question, Execute: false), ct);

    private async Task<QueryResponse> PostAsync(string path, QueryRequest body, CancellationToken ct)
    {
        using var resp = await http.PostAsJsonAsync(path, body, ct);
        if (!resp.IsSuccessStatusCode)
        {
            var detail = await resp.Content.ReadAsStringAsync(ct);
            throw new HttpRequestException($"DbRag API {(int)resp.StatusCode}: {detail}", null, resp.StatusCode);
        }
        return (await resp.Content.ReadFromJsonAsync<QueryResponse>(cancellationToken: ct))!;
    }
}

public static class DbRagServiceCollectionExtensions
{
    /// builder.Services.AddDbRagClient(builder.Configuration.GetSection("DbRag"));
    public static IServiceCollection AddDbRagClient(this IServiceCollection services, Microsoft.Extensions.Configuration.IConfiguration cfg)
    {
        services.Configure<DbRagOptions>(cfg);
        services.AddHttpClient<IDbRagClient, DbRagClient>((sp, c) =>
        {
            var o = sp.GetRequiredService<IOptions<DbRagOptions>>().Value;
            c.BaseAddress = new Uri(o.BaseUrl);
            c.DefaultRequestHeaders.Add("X-API-Key", o.ApiKey);
            c.Timeout = Timeout.InfiniteTimeSpan;                  // resilience handler owns timeouts
        })
        .AddStandardResilienceHandler(o =>                         // retry + circuit breaker + timeouts (Polly v8)
        {
            o.AttemptTimeout.Timeout = TimeSpan.FromSeconds(60);
            o.TotalRequestTimeout.Timeout = TimeSpan.FromSeconds(120);
            o.CircuitBreaker.SamplingDuration = TimeSpan.FromSeconds(120);   // must be >= 2x attempt timeout
            o.Retry.MaxRetryAttempts = 1;                          // LLM calls are slow: retry sparingly
            o.Retry.ShouldHandle = args => ValueTask.FromResult(
                args.Outcome.Exception is not null ||
                (int?)args.Outcome.Result?.StatusCode is 502 or 503 or 504);   // never retry 4xx / 429
        });
        return services;
    }
}
