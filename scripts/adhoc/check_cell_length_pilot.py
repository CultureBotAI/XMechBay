#!/usr/bin/env python3
"""2026-11-07: Reproduce the cell-length pilot for a METPO discussion issue.

Inputs: examples/cell-length/{observations.json,candidate.sssom.tsv,model.ofn,
trial-bridge.ofn}. Outputs: local reasoned OWL/logs and optional Markdown report.
Requires ROBOT, RDFLib and SSSOM; performs no network calls. No ontology is edited.
"""
import argparse
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys

from rdflib import Graph, Namespace, OWL, RDFS, URIRef


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'examples/cell-length'
OBO = Namespace('http://purl.obolibrary.org/obo/')
METPO = Namespace('https://w3id.org/metpo/')
EX = Namespace('https://example.org/cell-length-pilot/')


def run(command, log):
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    log.write_text(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(f'Command failed ({result.returncode}); inspect {log.name}')
    return result.stdout.strip()


def subsumed(graph, child, parent):
    """Named-class reachability over reasoner-materialized axioms, not a reasoner."""
    pending, seen = [child], set()
    while pending:
        current = pending.pop()
        if current == parent:
            return True
        if current in seen:
            continue
        seen.add(current)
        neighbors = list(graph.objects(current, RDFS.subClassOf))
        neighbors += list(graph.objects(current, OWL.equivalentClass))
        neighbors += list(graph.subjects(OWL.equivalentClass, current))
        pending.extend(x for x in neighbors if isinstance(x, URIRef))
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, default=ROOT / '.local/cell-length-pilot')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    checks = []

    def check(label, condition):
        checks.append((label, bool(condition)))
        if not condition:
            raise AssertionError(label)

    versions = {'ROBOT': run(['robot', '--version'], args.output_dir / 'version.txt'),
                'RDFLib': importlib.metadata.version('rdflib'),
                'SSSOM': importlib.metadata.version('sssom')}
    run([sys.executable, '-m', 'sssom.cli', 'validate',
         str(SOURCE / 'candidate.sssom.tsv'), '-V', 'JsonSchema',
         '-V', 'PrefixMapCompleteness', '-V', 'StrictCurieFormat'],
        args.output_dir / 'sssom.txt')
    check('SSSOM schema, prefix completeness, and strict CURIE validation', True)

    evidence = json.loads((SOURCE / 'observations.json').read_text())
    by_id = {obs['id']: obs for obs in evidence['observations']}
    comparison = evidence['comparison']
    subject = by_id[comparison['subject_observation']]
    reference = by_id[comparison['reference_observation']]
    percent = 100 * subject['mean'] / reference['mean']
    check('Comparison links two distinct measured observations', subject != reference)
    check('Both means are positive and the mutant mean is lower',
          0 < subject['mean'] < reference['mean'])
    check('Rounded mean ratio reproduces the reported 83.1 percent',
          round(percent, 1) == comparison['reported_percent_of_reference'])

    for mode in ('baseline', 'trial'):
        destination = args.output_dir / (mode + '.owl')
        command = ['robot']
        if mode == 'baseline':
            command += ['reason', '--input', str(SOURCE / 'model.ofn')]
        else:
            command += ['merge', '--input', str(SOURCE / 'model.ofn'),
                        '--input', str(SOURCE / 'trial-bridge.ofn'), 'reason']
        command += ['--reasoner', 'ELK', '--axiom-generators', 'SubClass EquivalentClass',
                    '--equivalent-classes-allowed', 'all', '--include-indirect', 'true',
                    '--output', str(destination)]
        run(command, args.output_dir / (mode + '.txt'))
        check(mode + ': reasoned artifact exists and is nonempty',
              destination.is_file() and destination.stat().st_size > 0)
        graph = Graph().parse(destination)
        below = lambda a, b: subsumed(graph, a, b)
        equivalent = lambda a, b: below(a, b) and below(b, a)
        check(mode + ': OBA cell length classifies under PATO length',
              below(OBO.OBA_2040145, OBO.PATO_0000122))
        check(mode + ': decreased phenotype classifies under cell-length phenotype',
              below(EX.RelativeDecreasedCellLengthPhenotype, EX.CellLengthPhenotype))
        check(mode + ': abnormal decreased phenotype classifies under decreased phenotype',
              below(EX.AbnormallyDecreasedCellLengthPhenotype, EX.RelativeDecreasedCellLengthPhenotype))
        check(mode + ': decreased summary does not entail abnormal qualifier',
              not below(EX.RelativeDecreasedCellLengthPhenotype, EX.AbnormallyDecreasedCellLengthPhenotype))
        check(mode + ': neutral phenotype does not entail decreased state',
              not below(EX.CellLengthPhenotype, EX.RelativeDecreasedCellLengthPhenotype))
        check(mode + ': attribute is not equivalent to phenotype wrapper',
              not equivalent(OBO.OBA_2040145, EX.CellLengthPhenotype))
        check(mode + ': METPO is not equivalent to decreased phenotype',
              not equivalent(METPO['1000881'], EX.RelativeDecreasedCellLengthPhenotype))
        if mode == 'baseline':
            check('baseline: definition source and closeMatch do not entail equivalence',
                  not equivalent(METPO['1000881'], OBO.OBA_2040145))
            check('baseline: METPO length classification is not supplied by the mapping',
                  not below(METPO['1000881'], OBO.PATO_0000122))
        else:
            check('trial: explicitly assumed bridge entails equivalence',
                  equivalent(METPO['1000881'], OBO.OBA_2040145))
            check('trial: bridge adds the PATO length classification',
                  below(METPO['1000881'], OBO.PATO_0000122))
            check('trial: bridge propagates METPO parent to OBA; review this consequence',
                  below(OBO.OBA_2040145, METPO['1000059']))

    lines = ['# Cell-length pilot verification', '',
             'Generated by `scripts/adhoc/check_cell_length_pilot.py`.', '',
             'Scope: focused axiom slice and hypothetical bridge, not complete ontology integration.',
             'Negative checks mean not entailed in this module; they do not assert biological disjointness.',
             'No numeric comparison, statistical significance, abnormality, or causal mechanism is inferred by OWL.', '',
             f'Derived from reported means: mutant/reference = {percent:.4f}%; difference = '
             f"{subject['mean'] - reference['mean']:.2f} um. SEM and P threshold remain source-reported.", '',
             'Tools: ' + '; '.join(f'{k} {v}' for k, v in versions.items()), '',
             '| Check | Result |', '|---|---|']
    lines += [f'| {label} | PASS |' for label, _ in checks]
    report = '\n'.join(lines) + '\n'
    if args.report:
        args.report.write_text(report)
    print(report)


if __name__ == '__main__':
    main()
