import { describe, it, expect, vi, beforeEach } from "vitest";

describe("dual-agent-memory openclaw plugin", () => {
  let plugin: (api: any) => void;

  // Collect all registrations
  let registeredTools: any[];
  let registeredHooks: any[];
  let registeredServices: any[];
  let registeredContextEngines: any[];
  let registeredClis: any[];
  let api: any;

  beforeEach(async () => {
    registeredTools = [];
    registeredHooks = [];
    registeredServices = [];
    registeredContextEngines = [];
    registeredClis = [];

    api = {
      config: {
        dbPath: ":memory:",
        autoEvaluate: false, // Disable hook for simpler tool-only tests
      },
      logger: {
        info: vi.fn(),
        warn: vi.fn(),
        error: vi.fn(),
      },
      registerTool: vi.fn((tool: any) => registeredTools.push(tool)),
      registerHook: vi.fn((_event: string, handler: any, opts: any) =>
        registeredHooks.push({ event: _event, handler, opts })
      ),
      registerService: vi.fn((svc: any) => registeredServices.push(svc)),
      registerContextEngine: vi.fn((_id: string, factory: any) =>
        registeredContextEngines.push({ id: _id, engine: factory() })
      ),
      registerCli: vi.fn((setup: any) => registeredClis.push(setup)),
    };

    const mod = await import("../src/index");
    plugin = mod.default;
  });

  it("registers all expected tools", () => {
    plugin(api);

    const toolNames = registeredTools.map((t) => t.name);
    expect(toolNames).toContain("memory_search");
    expect(toolNames).toContain("memory_evaluate");
    expect(toolNames).toContain("memory_briefing");
    expect(toolNames).toContain("memory_archive");
    expect(toolNames).toContain("memory_status");
    expect(registeredTools).toHaveLength(5);
  });

  it("registers the context engine", () => {
    plugin(api);
    expect(api.registerContextEngine).toHaveBeenCalledOnce();
    expect(registeredContextEngines[0].id).toBe("dual-agent-memory");
  });

  it("registers the consolidation service", () => {
    plugin(api);
    expect(registeredServices).toHaveLength(1);
    expect(registeredServices[0].id).toBe("dual-agent-memory-consolidator");
  });

  it("registers the CLI command", () => {
    plugin(api);
    expect(registeredClis).toHaveLength(1);
  });

  it("skips epistemic hook when autoEvaluate is false", () => {
    plugin(api);
    expect(registeredHooks).toHaveLength(0);
  });

  it("registers epistemic hook when autoEvaluate is true", () => {
    api.config.autoEvaluate = true;
    plugin(api);
    expect(registeredHooks).toHaveLength(1);
    expect(registeredHooks[0].opts.name).toBe("dual-agent-memory.epistemic-monitor");
  });

  it("memory_status tool returns expected shape", async () => {
    plugin(api);
    const statusTool = registeredTools.find((t) => t.name === "memory_status");
    const result = await statusTool.handler({});
    expect(result).toHaveProperty("archive_chunks");
    expect(result).toHaveProperty("observed_turns");
    expect(result).toHaveProperty("session_id");
    expect(result).toHaveProperty("config");
    expect(typeof result.archive_chunks).toBe("number");
  });

  it("memory_archive stores and memory_search retrieves chunks", async () => {
    plugin(api);
    const archiveTool = registeredTools.find((t) => t.name === "memory_archive");
    const searchTool = registeredTools.find((t) => t.name === "memory_search");

    // Archive some messages
    const archiveResult = await archiveTool.handler({
      messages: [
        { role: "user", content: "We decided to use PostgreSQL for the database." },
        { role: "assistant", content: "I've confirmed we will use PostgreSQL as our primary database." },
      ],
      session_id: "test-session-1",
    });

    expect(archiveResult.chunks_created).toBe(2);
    expect(archiveResult.chunks_after).toBeGreaterThan(0);

    // Search for them
    const searchResult = await searchTool.handler({ query: "PostgreSQL database" });
    expect(searchResult.total_results).toBeGreaterThan(0);
    expect(searchResult.results[0].content).toContain("PostgreSQL");
  });

  it("memory_evaluate detects contradictions", async () => {
    plugin(api);
    const archiveTool = registeredTools.find((t) => t.name === "memory_archive");
    const evalTool = registeredTools.find((t) => t.name === "memory_evaluate");

    // Archive a decision
    await archiveTool.handler({
      messages: [
        { role: "assistant", content: "We decided to use Redis for caching." },
      ],
      session_id: "test-session-2",
    });

    // Evaluate a contradicting statement
    const result = await evalTool.handler({
      output_text: "We decided to use Memcached for caching.",
    });

    expect(result.score).toBeLessThan(1.0);
    expect(typeof result.recommendation).toBe("string");
  });

  it("memory_briefing returns structured briefing", async () => {
    plugin(api);
    const archiveTool = registeredTools.find((t) => t.name === "memory_archive");
    const briefingTool = registeredTools.find((t) => t.name === "memory_briefing");

    await archiveTool.handler({
      messages: [
        { role: "assistant", content: "We decided to use TypeScript for the project." },
      ],
      session_id: "brief-session",
    });

    const result = await briefingTool.handler({ session_id: "new-session" });
    expect(result.briefing).toContain("Session Briefing");
    expect(result.session_id).toBe("new-session");
    expect(result.archive_size).toBeGreaterThan(0);
  });

  it("context engine has required methods", () => {
    plugin(api);
    const engine = registeredContextEngines[0].engine;
    expect(engine.info.id).toBe("dual-agent-memory");
    expect(engine.info.ownsCompaction).toBe(true);
    expect(typeof engine.bootstrap).toBe("function");
    expect(typeof engine.ingest).toBe("function");
    expect(typeof engine.assemble).toBe("function");
    expect(typeof engine.compact).toBe("function");
    expect(typeof engine.afterTurn).toBe("function");
    expect(typeof engine.prepareSubagentSpawn).toBe("function");
    expect(typeof engine.onSubagentEnded).toBe("function");
  });
});
