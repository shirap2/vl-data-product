# Use cases for a vehicle-location data product

The vehicle-location signal gives the exact position of the host vehicle in 3D, as a point cloud
rounded to whole centimetres, at 16 frames per second. These are the use cases considered for it. One is implemented.

| # | Use case | Who benefits | What it needs from the signal | Status |
|---|---|---|---|---|
| 1 | **Ego-motion extraction: velocity and acceleration** | Algorithm teams that validate motion estimates against exact vehicle motion | Position per frame, so velocity and acceleration follow from differences | **Implemented** |
| 2 | Search for driving behaviour (hard braking, reversing, stopping) | Teams building training sets from rare moments | Velocity and acceleration over time, plus rules that mark events | Considered |
| 3 | Training-data mining for detectors | Detection teams that need hard, rare moments | Event time ranges that a clip service can cut | Considered |
| 4 | Detection quality sliced by driving condition | Evaluation teams that ask whether a model gets worse while braking or reversing | A motion-condition label for every frame | Considered |
| 5 | Localization quality and drift | Map and localization teams comparing repeated drives of one route | Trajectories per drive | Considered |
| 6 | Dataset balancing and train/validation splits | Data teams avoiding near-identical drives on both sides of a split | Drive keys and motion summaries | Considered |

## Why use case 1

- **The warehouse stays generic.** Position per frame is useful to every other use case in the
  table, so the stored tables do not depend on this one.
- **It is the base of the others.** Most of cases 2 to 6 start from velocity and acceleration (case 5 needs trajectories and case 6
drive keys and summaries).

## What the implemented use case delivers

A user can ask, for any generated drive: how fast was the vehicle moving at each frame
(signed, so backward is negative), and how fast was that speed changing. The result is stored
in the motion table and can be queried with SQL or `VLClient`. The extraction is checked by
development measurements, not by the product: Module 3's sweep tool measures the estimator on
generated drives, and the generator's two-way check (`roundtrip`) shows that the scenario can be
recovered from the frames.
