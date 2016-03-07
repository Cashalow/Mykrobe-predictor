from __future__ import print_function
import logging
from atlas.utils import check_args
from atlas.typing import CoverageParser
from atlas.typing import Genotyper
from atlas.pheno import TBPredictor
from atlas.pheno import StaphPredictor
from atlas.pheno import GramNegPredictor
from atlas.metagenomics import AMRSpeciesPredictor
from pprint import pprint
import json

STAPH_PANELS = ["data/panels/Coagneg.fasta",
                "data/panels/Staphaureus.fasta",
                "data/panels/Saureus.fasta",
                "data/panels/Sepidermidis.fasta",
                "data/panels/Shaemolyticus.fasta",
                "data/panels/Sother.fasta",
                "data/panels/staph-amr-genes.fasta",
                "data/panels/staph-amr-mutations.fasta"]
GN_PANELS = [
    "data/panels/gn-amr-genes",
    "data/panels/Escherichia_coli",
    "data/panels/Klebsiella_pneumoniae",
    "data/panels/gn-amr-genes-extended"]


def run(parser, args):
    base_json = {args.sample: {}}
    args = parser.parse_args()
    hierarchy_json_file = None
    if args.panel is not None:
        if args.panel == "bradley-2015":
            TB_PANELS = [
                "data/panels/tb-species-160227.fasta",
                "data/panels/tb-amr-bradley_2015.fasta"]
        elif args.panel == "walker-2015":
            TB_PANELS = [
                "data/panels/tb-species-160227.fasta",
                "data/panels/tb-amr-walker_2015.fasta"]

    if not args.species:
        panels = TB_PANELS + GN_PANELS + STAPH_PANELS
        panel_name = "tb-gn-staph-amr"

    elif args.species == "staph":
        panels = STAPH_PANELS
        panel_name = "staph-amr"
        # hierarchy_json_file = "data/phylo/saureus_hierarchy.json"

    elif args.species == "tb":
        panels = TB_PANELS
        panel_name = "tb-amr"
        hierarchy_json_file = "data/phylo/mtbc_hierarchy.json"
    elif args.species == "gn":
        panels = GN_PANELS
        panel_name = "gn-amr"
    logging.info("Running AMR prediction with panels %s" % ", ".join(panels))
    base_json[args.sample]["panels"] = panels
    base_json[args.sample]["files"] = args.seq
    # Run Cortex
    cp = CoverageParser(
        sample=args.sample,
        panel_file_paths=panels,
        seq=args.seq,
        kmer=args.kmer,
        force=args.force,
        verbose=False,
        skeleton_dir=args.tmp)
    cp.run()
    # Detect species
    species_predictor = AMRSpeciesPredictor(
        phylo_group_covgs=cp.covgs.get(
            "complex",
            {}),
        sub_complex_covgs=cp.covgs.get(
            "sub-complex",
            {}),
        species_covgs=cp.covgs["species"],
        lineage_covgs=cp.covgs.get(
            "sub-species",
            {}),
        base_json=base_json[args.sample],
        hierarchy_json_file=hierarchy_json_file)
    species_predictor.run()

    # ## AMR prediction

    depths = []
    Predictor = None
    if species_predictor.is_saureus_present():
        depths = [species_predictor.out_json["phylogenetics"]
                  ["phylo_group"]["Staphaureus"]["median_depth"]]
        Predictor = StaphPredictor
    elif species_predictor.is_mtbc_present():
        depths = [species_predictor.out_json["phylogenetics"]["phylo_group"][
            "Mycobacterium_tuberculosis_complex"]["median_depth"]]
        Predictor = TBPredictor
    elif species_predictor.is_gram_neg_present():
        Predictor = GramNegPredictor
        try:
            depths = [species_predictor.out_json["phylogenetics"][
                "species"]["Klebsiella_pneumoniae"]["median_depth"]]
        except KeyError:
            depths = [species_predictor.out_json["phylogenetics"]
                      ["species"]["Escherichia_coli"]["median_depth"]]
    # pprint (species_predictor.out_json["phylogenetics"]["species"])
    # Genotype
    q = args.quiet
    args.quiet = True
    if depths:
        gt = Genotyper(sample=args.sample, expected_depths=depths,
                       variant_covgs=cp.variant_covgs,
                       gene_presence_covgs=cp.covgs["presence"],
                       base_json=base_json,
                       contamination_depths=[],
                       include_hom_alt_calls=True)
        gt.run()
    args.quiet = q
    if Predictor is not None:
        predictor = Predictor(variant_calls=gt.variant_calls,
                              called_genes=gt.gene_presence_covgs,
                              base_json=base_json[args.sample])
        predictor.run()

    print(json.dumps(base_json, indent=4))
