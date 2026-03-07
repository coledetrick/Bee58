B58 log analyzer

**todo**
(Grab your own manual log)
1. Local representation of the program
  1.1 Parse MHD CSV column names.
  1.2 Going to need to manually code the analysis, meaning we will need to know why we are looking for certain columns and why these columns matter, break down what the manual process is.  

Metric (MHD Column Name),Type,"The ""Golden"" Goal"
Ignition Correction Cyl 1-6,Reaction,0.0 across all cylinders. Anything more negative than -3.0 suggests knock or poor fuel.
Boost Pressure (Target vs Actual),Delta,"Actual within ±1.0 psi of Target. If Actual is much lower, check for a boost leak."
Rail Pressure (Target vs Actual),Delta,"Actual stays above 2,500 psi (Gen 1) or 2,900 psi (TU). Dips indicate a struggling fuel pump."

Accelerator Pedal Position (%): Use this as your "Filter." Only run diagnostics when this is >99%.

Engine Speed (RPM): Use this to define the "Pull Range" (typically 2,500 to 6,500 RPM).

Throttle Plate Angle (%): If the pedal is 100% but this drops to 80% or less, the ECU is "closing the throttle" to protect the engine from overboost or knock.

WGDC (Wastegate Duty Cycle) (%): Tells you how hard the turbo is working.

Logic: High WGDC + Low Boost = Boost Leak.


IAT (Intake Air Temperature): If IATs rise more than 20-30°F during a single pull, the water-to-air intercooler may be struggling.

STFT / LTFT (Short/Long Term Fuel Trims): If these are adding more than 15% fuel, you might have a vacuum leak or a scaling issue with the injectors.

Oil & Coolant Temp: Essential for "Sanity Checks." If a user does a pull while the oil is only 100°F (too cold), your bot should flag this as "Unsafe Operating Procedure."
