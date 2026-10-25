# Use cases for a vehicle-location data product

The vehicle-location signal gives the exact position of the host vehicle in 3D at 16
frames per second. These are the use cases considered for it. One is implemented.

| # | Use case | Who benefits | What it needs from the signal | Status |
|---|---|---|---|---|
| 1 | **Ego-motion extraction: velocity and acceleration** | Algorithm teams that validate motion estimates against exact vehicle motion | Position per frame, so velocity and acceleration follow from differences | **Implemented** |
| 2 | Search for driving behaviour (hard braking, reversing, stopping) | Teams building training sets from rare moments | Velocity and acceleration over time, plus rules that mark events | Considered; dropped from scope |
| 3 | Training-data mining for detectors | Detection teams that need hard, rare moments | Event time ranges that a clip service can cut | Considered |
| 4 | Detection quality sliced by driving condition | Evaluation teams that ask whether a model gets worse while braking or reversing | A motion-condition label for every frame | Considered |
| 5 | Localization quality and drift | Map and localization teams comparing repeated drives of one route | Trajectories per drive | Considered |
| 6 | Dataset balancing and train/validation splits | Data teams avoiding near-identical drives on both sides of a split | Drive keys and motion summaries | Considered |

## Why use case 1

- **It has natural ground truth.** The generator knows the exact motion, so correctness can be
  measured instead of argued.
- **The warehouse stays generic.** Position per frame is useful to every other use case in the
  table, so the stored tables do not depend on this one.
- **It is the base of the others.** Cases 2 to 6 all start from velocity and acceleration.

## What the implemented use case delivers

A user can ask, for any generated drive: how fast was the vehicle moving at each frame
(signed, so backward is negative), and how fast was that speed changing. The result can be
compared with the ground truth for the same scenario, which is how the extraction is shown to
be correct.
