# LLM Usage

Written by hand. The full log of 162 prompts is not included: this file keeps the main ones.

- **Model:** Claude Sonnet 5.5 (`claude-sonnet-5-5`), through Claude Code. Four Claude sessions worked in parallel git
  worktrees, one per module (generator, loader and ETL, use case, BI). They coordinated through one
  architecture document, agreed file contracts and short messages, and I approved every commit.
- **Approach:** plan-loop for the architecture and for each module, one implement-loop per task
  (tests first, reviewer agents for correctness, security and hygiene, a real run as proof), then my
  review before each commit. Pure deletions used a lighter route: delete, run the full suite, ruff,
  mypy and coverage, search for the removed names, one reviewer pass, then a real run.
- **Skills used:** ForgeLoopP (my own plugin), version 0.19.0. The stage prompts and reviewer agents
  that run inside each loop are versioned in its repo.
- **What I changed or rejected:**
  - Dropped yaw and turning: a cube's orientation cannot be read from its points, so motion is a
    straight line along x with a signed speed.
  - Narrowed the use case to velocity and acceleration; dropped events and clip candidates.
  - Replaced the assistant's four segment types with two: `move` and `stop`.
  - Chose a canonical-JSON scenario hash and byte-level file hashes.
  - Chose the real density, 100 x 100 x 100 points per frame, over the 10 x 10 x 10 sample the
    assistant first used, with zstd storage and a cap of 2,000 frames per scenario.
  - Rejected guessed tolerances: the estimator's window and limits were measured on generated drives.
  - Decided that the assignment does not ask for ground truth, so I had the ground-truth file and the
    evaluation removed from all four modules (26 Oct).
  - Hid the seed from the Generate tab (it stays in the hash), and added an SQL tab to the app.
  - Chose a read-only Python SDK plus SQL over a web service.

## Main prompts

Eight of the prompts that shaped the product, as sent (typos kept). Each plan or task ran as a loop: plan, tests first, reviewer agents, a real run, then my approval.

### 1. Start of the loader and ETL module

2026-10-23

```text
open a new brnach and work tree and start the plan loop for loader/ etl- follow the archetecture context txt file
```

### 2. Defines the data flow: full point cloud in, essential data out, queryable lake

2026-10-23

```text
you are getting the full point cloud and then we should etl it to only the essential data needed for my use case which is infering velocity and acceration. then it will be saved to the disk in a datalake that can be queuried. make sure that this matches the instructions in the assignmnet.pdf file
```

### 3. Decides to keep the real density (1,000,000 points per frame) instead of a thinned sample

2026-10-24

```text
i want to use the real density- it is important to me to follow the instructions in the pdf. how do reccomend we do this? is it possible to keep all the cloud points and not thin the generator results? why does it need streaming? is it complicated to implement?
do you think we should chnge assumpton a54? why did you reccomend to only keep the x?
```

### 4. Corrects the design: point cloud in, thin format out, generic warehouse

2026-10-25

```text
we have to modify the design, the input should be the point clound and the output should be a thin data format that is good for our use case which is calculating velocity and acceleration along the frames, and save it in a data wharehose that is generic data stracture. is that our design right now? if not - plan loop the modification for a design like described above
```

### 5. Asks for a check of the plan against the assignment

2026-10-25

```text
review the assigmnet doc and make sure that all that you have imleented up until now and your plan file is aligned with it and answers the requirements. if you find anything missing or inaccurate tell me. use assignmnet.pdf
```

### 6. Plan-loop prompt for the use case, SDK and queries (shortened here; the full prompt also lists decided facts and file contracts)

2026-10-24

```text
<pasted_content id="99f5">
/plan-loop Module 3: use-case algorithm (velocity and acceleration from the lake), including the SDK and the evaluation step.

Read first, in this order:
1. docs/architecture_context.txt, sections "DECISIONS", "MODULE 3", "ASSIGNMENT COVERAGE".
2. docs/ASSUMPTIONS.md (decisions D1 to D13, M2-D1 to M2-D11, assumptions A1 to A52 and M2-A1 to M2-A25).
3. The approved Module 2 plan: .claude/plans/module2-loader-etl.md, on branch worktree-module2-loader-etl (worktree .claude/worktrees/module2-loader-etl). Module 2 is partly built there and not merged to main yet.
4. The approved Module 1 plan: .claude/plans/module1-data-generator.md.

Goal: from the `keypoints` table in the lake, compute the ego vehicle's velocity and acceleration, write a `motion` table to the lake, expose queries through a Python SDK (VLClient) plus SQL, and evaluate the result against the generator's ground truth. Use case: the vehicle moves in a straight line along x, forwards, backwards and stopped. No yaw, no turning, no events.
[...shortened]
```

### 7. Asks for the engineering report with a module-by-module structure

2026-10-26

```text
i want to create an engineering report about the project. we have the main project with each one of the modules as a black box- module 1- generator, module 2- etl+loarder, module 3 and module 4 bi. we should explain in the report the following points: 1. file structure- detailed file structure of the project, what each file does. 2. a block diagram showing the flow and the interactions between the different clases and modules in the project for the main project system design we will show just a high level diagram showing just the flow of the project- inpputs andoutputs from the diifferent modules. we will add later a deeper explanation in a high resolution for each one of the modules. we will do that for each one in a different tab later. 3. add a text box explaining the context of the porject, what exactly it does. under the text box add another text box explaining the engineering concepts and assuptions we used along the way for the design. star with the main tab pshowing the genearl context of the rject and ill guide you and tell you what to fix for the other modules. use the artifact skill
```

### 8. Removes ground truth and evaluation from all modules

2026-10-26

```text
the ground truth and evaluation using the ground truth is not part of the assignment- confirm this in the @assignment.pdf docs. if this is the case remove it from all modules and docs files
```
