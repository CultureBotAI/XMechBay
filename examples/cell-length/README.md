# Worked microbial cell-length pilot

This discussion example connects a measured comparison, a proposed SSSOM mapping,
and a focused OWL classification test. It changes no METPO, OBA, or uPheno ontology.
All `ex:` concepts are local proposals under `https://example.org/cell-length-pilot/`.

## Published observation and explicit comparator

[Tsang and Bernhardt (2015), Table 1](https://doi.org/10.1111/mmi.12905) reports:

| Study strain | Perturbation | Mean cell length | SEM |
|---|---|---:|---:|
| E. coli TB28, study wild-type comparator | — | 4.37 µm | 0.06 µm |
| E. coli MT10 | Chromosomal ftsL(E88K), called ftsL* | 3.63 µm | 0.01 µm |

Both use the MG1655 background, LB containing 0.5% NaCl, and 30°C. The table
footnote specifies midlog precultures, backdilution to OD600 approximately 0.02,
measurement at OD600 approximately 0.3–0.5, phase-contrast microscopy and
MicrobeTracker, 600 cells per condition, and n=3. Preserve that sampling wording:
replicate type and whether the cell count is total or per replicate are not
established here. The table reports 83.1% of wild-type length and P<0.01. SEM is
not SD. The precise statistical test is not established in this extraction.

[observations.json](observations.json) retains both measurements, their shared
conditions, source locator, and a comparison referring to both observation IDs.
The script computes 83.0664% from the rounded means, agreeing with the reported
83.1%, and a difference of −0.74 µm. It does not recompute a significance test.

The interpretation is **decreased mean cell length relative to TB28 under these
conditions**. It is a population-summary annotation, not an assertion that every
MT10 cell is shorter, that the effect holds in every medium, or that all E. coli
have this phenotype. `candidate_summary_concept` links the comparison to a local
phenotype concept for review; it does not create an OWL class assertion about
individual cells. Numeric comparison and comparator selection remain application
logic and observation metadata.

Primary full text: [PMC article and table](https://pmc.ncbi.nlm.nih.gov/articles/PMC4414402/#T1).
The complete table is also available from the public
[NCBI BioC XML endpoint](https://www.ncbi.nlm.nih.gov/research/bionlp/RESTful/pmcoa.cgi/BioC_xml/PMC4414402/unicode).
The tabulated values are transcribed source data, not invented fixtures or values
estimated from a plot.

## Existing METPO–OBA connection

`METPO:1000881` **cell length** already cites `OBA:2040145` in the annotation on
its definition. This is a worked follow-up to that existing reference:

```text
METPO:1000881 SubClassOf METPO:1000059

OBA:2040145 EquivalentTo
  PATO:0000122 and (RO:0000052 some CL:0000000)
```

Here PATO:0000122 is length, CL:0000000 is cell, and RO:0000052 is
characteristic of/inheres in. METPO's wording describes the longer dimension
viewed in a plane; whether that is coextensive with the OBA definition requires
curator review. METPO's phenotype parent is a quality concept; it must not be
silently equated with a uPheno-style phenotype wrapper.

[candidate.sssom.tsv](candidate.sssom.tsv) proposes **skos:closeMatch**, with source
commits, date, labels, and an explicit pending-review comment. Its unspecified
matching justification avoids claiming human curation or reasoner-proven
equivalence. This candidate is not an approved interchange mapping. No existing
species-neutral UPHENO identifier for this cell-length state was verified, so
none is invented. No mapping is made between the neutral attribute and the
decreased state.

## Relative decrease and abnormality are separate

[model.ofn](model.ofn) defines these local experimental concepts:

```text
CellLengthPhenotype ≡ has_part some OBA:2040145
RelativeDecreasedCellLengthPhenotype ≡ has_part some (
  PATO:0000574 and (RO:0000052 some CL:0000000))
AbnormallyDecreasedCellLengthPhenotype ≡ has_part some (
  PATO:0000574 and (RO:0000052 some CL:0000000)
  and (RO:0002573 some PATO:0000460))
```

PATO:0000574 is decreased length and is a subclass of length. The last expression
instantiates the structure of the cited uPheno abnormal-decreased-length pattern
with cell; it is a **local proposal**, not a new UPHENO term. The relative concept
omits the abnormal modifier and is therefore not asserted to be uPheno-conformant.
Neither expression stores the actual TB28 comparator; that remains in the linked
comparison record. A lower group mean, even when reported statistically
significant, does not automatically justify an abnormal qualifier.

## Reasoning experiment and limits

The baseline contains the definition-source annotation and the candidate
closeMatch annotation, not a METPO–OBA equivalence axiom. It is a small,
hand-selected axiom slice: unrelated parents, disjointness, imports, and other
source content are deliberately omitted. The annotated METPO definition retains
its definition-source structure. This is not a complete source module or a
consistency assessment of the full combined ontologies.

[trial-bridge.ofn](trial-bridge.ofn) **hypothetically assumes**
`EquivalentClasses(METPO:1000881 OBA:2040145)` to inspect consequences. The trial
does not prove the assumption scientifically correct and must not be loaded into
a production ontology automatically. In particular, equivalence propagates
METPO:1000059 as a parent of OBA:2040145. This consequence needs review alongside
the benefit of classifying METPO cell length under PATO length.

[verification.md](verification.md) records the executed checks. Both models
classify abnormal decreased → relative decreased → cell-length phenotype.
Neither entails relative decreased → abnormal or neutral → decreased, and neither
equates the neutral quality with the phenotype wrapper. These are
**non-entailments in this module**, not claims of biological disjointness.
The existing source citation and closeMatch do not entail logical equivalence.
Only the explicit hypothetical bridge introduces it. No mechanism edge is
generated; the numerical comparison does not itself establish why length changes.

## Reproduce

Use Python 3.10+, ROBOT with Java, RDFLib, and the SSSOM toolkit. Versions actually
used are recorded in the report. From the repository root:

```bash
python3 scripts/adhoc/check_cell_length_pilot.py --report examples/cell-length/verification.md
```

The script runs SSSOM schema/prefix/CURIE validation, verifies the comparison,
and calls ROBOT/ELK for the baseline and hypothetical bridge. Reasoned artifacts
and command logs are kept in ignored `.local/cell-length-pilot/`. Failed commands
or assertions stop the run; successful exit alone is not the check, as output
files are parsed and their classifications examined. No network is required.

## Pinned ontology provenance

- [METPO source](https://github.com/berkeleybop/metpo/blob/800c9d27913158efdf466edf2cd710dbf1ce893f/metpo.owl),
  commit `800c9d27913158efdf466edf2cd710dbf1ce893f`, ontology release 2026-06-12.
- [OBA entity-attribute data](https://github.com/obophenotype/bio-attribute-ontology/blob/c08bcfd6061157c3aae6ef854526e941942859c4/src/patterns/data/default/entity_attribute.tsv)
  and [pattern](https://github.com/obophenotype/bio-attribute-ontology/blob/c08bcfd6061157c3aae6ef854526e941942859c4/src/patterns/dosdp-patterns/entity_attribute.yaml),
  commit `c08bcfd6061157c3aae6ef854526e941942859c4`.
- [PATO source](https://github.com/pato-ontology/pato/blob/f16927f80599f74d7a974ab3567a78e5420691ea/pato.obo),
  commit `f16927f80599f74d7a974ab3567a78e5420691ea`.
- [uPheno decreased-length pattern](https://github.com/obophenotype/upheno/blob/5b92e2e1cb9aa254a3ec99184346e656f831bc25/src/patterns/dosdp-dev/abnormallyDecreasedLengthOfAnatomicalEntity.yaml),
  commit `5b92e2e1cb9aa254a3ec99184346e656f831bc25`.
- [SSSOM model and predicate meanings](https://mapping-commons.github.io/sssom/1.0/spec-model/).

## Questions for METPO maintainers

1. Is the existing OBA definition-source reference sufficient to justify a curated
   exact mapping, or should the planar wording/axioms be revised first?
2. Which representation should be canonical for the measured observation and its
   relative phenotype: property, class, or both with an explicit bridge?
3. Is this a useful first case for the curated SSSOM work before growth traits,
   keeping relative decrease distinct from an abnormal phenotype?

Relevant discussions: METPO [#44](https://github.com/berkeleybop/metpo/issues/44),
[#344](https://github.com/berkeleybop/metpo/issues/344),
[#228](https://github.com/berkeleybop/metpo/issues/228),
[#350](https://github.com/berkeleybop/metpo/issues/350), and
[#351](https://github.com/berkeleybop/metpo/issues/351).
