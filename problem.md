# Research Problem

## Core Research Question

Given a fixed language model, a fixed set of repositories and issue-resolution tasks, and a fixed token budget, can better repository context, more reliable task execution, and experience-driven post-training improve:

1. the issue resolution rate, measured by repository tests or task-specific verifiers;
2. the cost efficiency, measured by tokens, execution time, and cost per resolved issue?

These methods will be introduced and evaluated in separate stages. A later component should only be added when the results from an earlier stage show why it is needed.

## Target Workflow

The eventual system will have the following interface:

- Input: a repository and an issue or another verifiable coding task;
- Output: a code patch, test results, and a complete execution record.

This is the final project target, not the scope of Stage 1.

## Stage 1: Minimal In-Process Job Executor

### Purpose

Build the smallest observable execution core before introducing models, repository understanding, or external infrastructure.

### Input

- a unique job ID;
- an executable Python function.

### Output

- the final job state;
- the execution result or error;
- timestamps and state-transition records.

### Goals

1. Implement the following state transitions:

   `pending → running → succeeded/failed`

2. Reject invalid state transitions.
3. Record execution errors without terminating the executor.
4. Allow the executor to continue running later jobs after one job fails.
5. Verify the behavior with automated tests.

### Acceptance Criteria

The following cases must be covered by tests:

- a job succeeds;
- a job raises an exception and becomes `failed`;
- an invalid state transition is rejected;
- a completed job cannot be executed again accidentally.

All tests must pass through one documented command.

### Out of Scope for Stage 1

- language models;
- repository and issue understanding;
- automatic code modification;
- memory;
- persistent databases;
- Docker;
- Go;
- reinforcement learning;
- recovery after the entire executor process crashes.

These items are deferred, not permanently excluded. They will be introduced in later stages only when the project has a concrete need for them.