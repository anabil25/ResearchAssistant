import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { FoundryAgentCatalog, PromptAgentBuilder } from "@/components/foundry-agent-studio";
import {
  attachPromptCapability,
  createPromptAgentDraft,
  getFoundryAgentInventory,
  getFoundryProjectContext,
  getFoundryProjectModels,
  getCapabilityDiscovery,
  savePromptAgentDraft,
} from "@/lib/api";
import { ApiError } from "@/lib/api";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    attachPromptCapability: jest.fn(),
    createPromptAgentDraft: jest.fn(),
    getFoundryAgentInventory: jest.fn(),
    getFoundryProjectContext: jest.fn(),
    getFoundryProjectModels: jest.fn(),
    getCapabilityDiscovery: jest.fn(),
    savePromptAgentDraft: jest.fn(),
  };
});

const getInventoryMock = getFoundryAgentInventory as jest.MockedFunction<typeof getFoundryAgentInventory>;
const getContextMock = getFoundryProjectContext as jest.MockedFunction<typeof getFoundryProjectContext>;
const getModelsMock = getFoundryProjectModels as jest.MockedFunction<typeof getFoundryProjectModels>;
const getCapabilityDiscoveryMock = getCapabilityDiscovery as jest.MockedFunction<typeof getCapabilityDiscovery>;
const createDraftMock = createPromptAgentDraft as jest.MockedFunction<typeof createPromptAgentDraft>;
const attachCapabilityMock = attachPromptCapability as jest.MockedFunction<typeof attachPromptCapability>;
const saveDraftMock = savePromptAgentDraft as jest.MockedFunction<typeof savePromptAgentDraft>;

beforeEach(() => {
  jest.resetAllMocks();
});

test("loads the configured Foundry project inventory", async () => {
  getInventoryMock.mockResolvedValue([
    {
      name: "research-coordinator",
      agent_type: "hosted",
      description: "Routes research work.",
      version: "2",
      status: "active",
      model: "gpt-5.4-mini",
    },
    {
      name: "literature-helper",
      agent_type: "prompt",
      description: "Summarizes literature.",
      version: "1",
      status: "active",
      model: "gpt-5.6-sol",
    },
  ]);

  render(<FoundryAgentCatalog onCreatePrompt={jest.fn()} />);

  expect(await screen.findByText("research-coordinator")).toBeInTheDocument();
  expect(screen.getByText("literature-helper")).toBeInTheDocument();
  expect(getInventoryMock).toHaveBeenCalledWith();
});

test("presents an unconfigured Foundry project as a state to fix, not a failure to retry", async () => {
  getInventoryMock.mockRejectedValue(
    new ApiError("No Foundry project endpoint is configured; agent inventory is unavailable.", 503),
  );

  render(<FoundryAgentCatalog onCreatePrompt={jest.fn()} />);

  // A missing deployment setting cannot be retried away, so the retryable
  // "Degraded" treatment reserved for real outages must not be used.
  expect(await screen.findByText("Not available yet")).toBeInTheDocument();
  expect(screen.queryByText("Degraded")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /retry/i })).not.toBeInTheDocument();
});

test("shows the authorization state instead of an unusable builder form", async () => {
  getContextMock.mockRejectedValue(new ApiError("An authenticated platform identity is required.", 401));

  render(<PromptAgentBuilder onViewAgents={jest.fn()} />);

  expect(await screen.findByText("Not authorized")).toBeInTheDocument();
  expect(screen.getByText("An authenticated platform identity is required.")).toBeInTheDocument();
  expect(screen.queryByRole("textbox", { name: "Agent ID" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Save prompt draft" })).not.toBeInTheDocument();
});

test("saves a prompt draft with the chosen model and approved Code Interpreter binding", async () => {
  const user = userEvent.setup();
  const onViewAgents = jest.fn();
  getContextMock.mockResolvedValue({ project_id: "foundry-project" });
  getModelsMock.mockResolvedValue([
    {
      deployment_name: "gpt-5.4-mini",
      model_name: "gpt-5.4-mini",
      model_format: "OpenAI",
      capacity: 30,
    },
  ]);
  getCapabilityDiscoveryMock.mockResolvedValue({
    descriptors: [
      {
        id: "foundry.code_interpreter",
        version: "1",
        provider: "microsoft_foundry",
        title: "Code Interpreter",
        description: "Runs Python in a managed sandbox.",
        operations: [
          {
            name: "run",
            maturity: "ga",
            lifecycle: "active",
            operation_class: "write_reversible",
            side_effect_destinations: ["foundry_sandbox"],
            requires_approval: false,
            reason: null,
            source_url: null,
            source_version: null,
            last_verified_at: null,
            input_schema_digest: null,
            output_schema_digest: null,
          },
        ],
        auth_requirements: [],
        risk_tier: "medium",
        data_boundary: "project",
        managed_foundry_native: true,
      },
    ],
    instances: [],
    warnings: [],
    refreshed_at: null,
  });
  createDraftMock.mockResolvedValue({
    logical_agent_id: "agent-literature-helper",
    etag: "draft-etag",
    manifest: {
      instructions: "",
      capabilities: [],
      model_deployment: null,
    },
  });
  attachCapabilityMock.mockResolvedValue({
    binding_id: "binding-1",
    descriptor_ref: { id: "foundry.code_interpreter" },
    operation_ref: { id: "run" },
  });
  saveDraftMock.mockResolvedValue({
    logical_agent_id: "agent-literature-helper",
    etag: "draft-etag-2",
    manifest: {
      instructions: "Use approved sources only.",
      capabilities: [],
      model_deployment: {
        deployment_name: "gpt-5.4-mini",
        model_name: "gpt-5.4-mini",
        model_format: "OpenAI",
        capacity: 30,
      },
    },
  });

  render(<PromptAgentBuilder onViewAgents={onViewAgents} />);
  await screen.findByRole("option", { name: "gpt-5.4-mini (gpt-5.4-mini)" });

  await user.clear(screen.getByLabelText("Agent ID"));
  await user.type(screen.getByLabelText("Agent ID"), "agent-literature-helper");
  await user.type(screen.getByLabelText("Display name"), "Literature helper");
  await user.type(screen.getByLabelText("Instructions"), "Use approved sources only.");
  await user.click(screen.getByRole("checkbox", { name: /Code Interpreter/i }));
  await user.click(screen.getByRole("button", { name: "Save prompt draft" }));

  await screen.findByText(/Draft saved/i);
  expect(onViewAgents).not.toHaveBeenCalled();
  expect(attachCapabilityMock).toHaveBeenCalledWith({
    descriptor_id: "foundry.code_interpreter",
    operation: "run",
  });
  expect(saveDraftMock).toHaveBeenCalledWith(
    expect.objectContaining({
      logical_agent_id: "agent-literature-helper",
      manifest: expect.objectContaining({
        instructions: "Use approved sources only.",
        model_deployment: expect.objectContaining({ deployment_name: "gpt-5.4-mini" }),
        capabilities: [
          expect.objectContaining({
            descriptor_ref: { id: "foundry.code_interpreter" },
          }),
        ],
      }),
    }),
  );
});

test("retries a failed save without creating another draft", async () => {
  const user = userEvent.setup();
  getContextMock.mockResolvedValue({ project_id: "foundry-project" });
  getModelsMock.mockResolvedValue([
    {
      deployment_name: "gpt-5.4-mini",
      model_name: "gpt-5.4-mini",
      model_format: "OpenAI",
      capacity: 30,
    },
  ]);
  getCapabilityDiscoveryMock.mockResolvedValue({
    descriptors: [],
    instances: [],
    warnings: [],
    refreshed_at: null,
  });
  createDraftMock.mockResolvedValue({
    logical_agent_id: "agent-retry-draft",
    etag: "draft-etag",
    manifest: { instructions: "", capabilities: [], model_deployment: null },
  });
  saveDraftMock
    .mockRejectedValueOnce(new Error("save failed"))
    .mockResolvedValueOnce({
      logical_agent_id: "agent-retry-draft",
      etag: "draft-etag-2",
      manifest: {
        instructions: "Keep it grounded.",
        capabilities: [],
        model_deployment: {
          deployment_name: "gpt-5.4-mini",
          model_name: "gpt-5.4-mini",
          model_format: "OpenAI",
          capacity: 30,
        },
      },
    });

  render(<PromptAgentBuilder onViewAgents={jest.fn()} />);
  await screen.findByRole("option", { name: "gpt-5.4-mini (gpt-5.4-mini)" });
  await user.clear(screen.getByLabelText("Agent ID"));
  await user.type(screen.getByLabelText("Agent ID"), "agent-retry-draft");
  await user.type(screen.getByLabelText("Display name"), "Retry draft");
  await user.type(screen.getByLabelText("Instructions"), "Keep it grounded.");
  await user.click(screen.getByRole("button", { name: "Save prompt draft" }));
  await screen.findByText("save failed");
  await user.click(screen.getByRole("button", { name: "Save changes" }));

  await screen.findByText(/Draft saved/i);
  expect(createDraftMock).toHaveBeenCalledTimes(1);
  expect(saveDraftMock).toHaveBeenCalledTimes(2);
});