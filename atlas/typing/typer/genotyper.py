from __future__ import print_function
import os
import json
from pprint import pprint
import csv
import glob
from mongoengine import connect
from mongoengine import DoesNotExist
import subprocess

from atlas.typing import TypedVariant
from atlas.typing import TypedPresence
from atlas.typing.typer.presence import PresenceTyper
from atlas.typing.typer.variant import VariantTyper
from atlas.vcf2db import VariantPanel
from atlas.vcf2db import CallSet

def get_params(url):
    params = {}
    try:
        p_str = url.split("?")[1]
    except IndexError:
        return params
    p_str = p_str.split('&')
    for p in p_str:
        k,v = p.split("=")
        params[k] = v
    return params

def max_pnz_threshold(vp):
    t =  max(100 - 2 * math.floor(float(max([len(alt) for alt in vp.alts])) / 100), 30)
    return t

class CortexGeno(object):

  def __init__(self, args):
    self.args = args
    if not args.panel:
        args.panel = "panel-%s-%i" % (args.db_name, args.kmer)  

  def run(self):
      self._check_panels() 
      self._run_cortex() 

  def _run_cortex(self):
      ## If ctx binary does not exist then build it
      self._build_panel_binary_if_required()
      ## Now get coverage on panel
      self._run_coverage_if_required()

  def _build_panel_binary_if_required(self):
      if not os.path.exists(self.ctx_skeleton_filepath) or self.args.force:
          if os.path.exists(self.ctx_skeleton_filepath):
              os.remove(self.ctx_skeleton_filepath)        
          subprocess.check_output(["/home/phelimb/git/mccortex/bin/mccortex31", "build", "-q",
                                   "-k", str(self.args.kmer), "-s", "%s" % self.args.panel,
                                   "-1", self.panel_filepath, self.ctx_skeleton_filepath]) 

  def _run_coverage_if_required(self):
      if not os.path.exists(self.ctx_tmp_filepath) or not os.path.exists(self.covg_tmp_file_path) or self.args.force:
          if os.path.exists(self.ctx_tmp_filepath):
              os.remove(self.ctx_tmp_filepath)
          if os.path.exists(self.covg_tmp_file_path):
              os.remove(self.covg_tmp_file_path)      
          subprocess.check_output(self.coverages_cmd)
      else:
          # print "Warning: Using pre-built binaries. Run with --force if panel has been updated."
          pass     

  @property 
  def coverages_cmd(self):
      cmd = ["/home/phelimb/git/mccortex/bin/mccortex31", "geno", "-q",
             "-I", self.ctx_skeleton_filepath,
             "-k", str(self.args.kmer), "-s", self.sample,
             "-o", self.covg_tmp_file_path]
      for seq in self.args.seq:
          cmd.extend(["-1", seq])
      cmd.extend(["-c", self.panel_filepath, self.ctx_tmp_filepath])
      return cmd 

  @property 
  def sample(self):
      return "-".join([self.args.sample, self.args.db_name, str(self.args.kmer)])

  @property
  def ctx_tmp_filepath(self):
    return "/tmp/%s_%s.ctx" % (self.sample, self.args.panel)

  @property
  def covg_tmp_file_path(self):
      return "/tmp/%s_%s.covgs" % (self.sample, self.args.panel)

  @property
  def panel_filepath(self):
      return os.path.abspath("data/panels/%s.fasta" % self.args.panel)       

  @property 
  def ctx_skeleton_filepath(self):
    return os.path.abspath("data/skeletons/%s_%i.ctx" % (self.args.panel, self.args.kmer)) 


class Genotyper(CortexGeno):

  def __init__(self, args):
    self.args = args
    self.variant_covgs = {}
    self.gene_presence_covgs = {}
    self.out_json = {self.args.sample : {}}   
    if not args.panel:
        args.panel = "panel-%s-%i" % (args.db_name, args.kmer)

  def run(self):
      self._connect_to_db()      
      self._set_up_db()       
      if self.args.force or not os.path.exists(self.covg_tmp_file_path):
          self._check_panels() 
          self._run_cortex()  
      self._parse_covgs()       
      self._type()    
      print(json.dumps(self.out_json,
                        indent=4, separators=(',', ': ')))           
      # self._insert_to_db()

  def _type(self):
      self._type_genes()
      self._type_variants()

  def _type_genes(self):
      gt = PresenceTyper(depths = [100])
      gene_presence_typed = gt.type(self.gene_presence_covgs)

      self.out_json[self.args.sample]["typed_presence"] = {}
      out_json = self.out_json[self.args.sample]["typed_presence"] 
      out_json = [gv.to_dict() for gv in gene_presence_typed]

  def _type_variants(self):
      gt = VariantTyper(depths = [100]) 
      typed_variants = gt.type(self.variant_covgs)
      self.out_json[self.args.sample]["typed_variants"] = {}
      out_json = self.out_json[self.args.sample]["typed_variants"] 
      for name, tvs in typed_variants.iteritems():
          for tv in tvs:
              try:
                  out_json[name].append(tv.to_dict())
              except KeyError:
                  out_json[name] = tv.to_dict()
    

  def _connect_to_db(self):
    connect('atlas-%s-%i' % (self.args.db_name ,self.args.kmer))

  def _set_up_db(self):
    try:
        self.call_set = CallSet.objects.get(name = self.args.sample + "_%s" % self.args.name)
    except DoesNotExist:
        self.call_set = CallSet.create(name = self.args.sample  + "_%s" % self.args.name, sample_id = self.args.sample)
    ## Clear any genotyped calls so far
    TypedVariant.objects(call_set = self.call_set).delete()

  def _check_panels(self):
      ## If panel does not exists then build it
      if not os.path.exists(self.panel_filepath):
          raise ValueError("Could not find a panel at %s." % self.panel_filepath)                                                 

  def _parse_summary_covgs_row(self, row):
      return row[0], int(row[2]), 100*float(row[3])

  def _parse_covgs(self):
      with open(self.covg_tmp_file_path, 'r') as infile:
          self.reader = csv.reader(infile, delimiter = "\t")
          for row in self.reader:
              allele, median_depth, percent_coverage = self._parse_summary_covgs_row(row)
              allele_name = allele.split('?')[0]
              if self._is_variant_panel(allele_name):
                  self._parse_variant_panel(row)
              else:
                  self._parse_seq_covgs(row)

  def _is_variant_panel(self, allele_name):
      alt_or_ref, _id = allele_name.split('-')
      return bool(alt_or_ref)

  def _parse_seq_panel(self, row):
      allele, median_depth, percent_coverage = self._parse_summary_covgs_row(row)
      allele_name = allele.split('?')[0]    
      if percent_coverage > 0:
          params = get_params(allele)
          gp = TypedPresence.create_object(name = params.get('name'),
                       version = params.get('version', 'N/A'),
                       percent_coverage = percent_coverage,
                       median_depth = median_depth
                       )
          try:
              self.gene_presence_covgs[gp.name][gp.version] = gp
          except KeyError:
              self.gene_presence_covgs[gp.name] = {}
              self.gene_presence_covgs[gp.name][gp.version] = gp

  def _parse_variant_panel(self, row):
      allele, reference_median_depth, reference_percent_coverage = self._parse_summary_covgs_row(row)
      allele_name = allele.split('?')[0].split('-')[1]
      params = get_params(allele)   
      num_alts = int(params.get("num_alts"))
      for i in range(num_alts):
          row = self.reader.next()
          allele, alternate_median_depth, alternate_percent_coverage = self._parse_summary_covgs_row(row)
          if alternate_percent_coverage > 30:
              tv = TypedVariant.create_object(
                                          name = allele_name,
                                          call_set = self.call_set,
                                          reference_percent_coverage = reference_percent_coverage, 
                                          alternate_percent_coverage = alternate_percent_coverage,
                                          reference_median_depth = reference_median_depth, 
                                          alternate_median_depth = alternate_median_depth,
                                          alt_name = "_".join([params.get("gene"), params.get("mut")]),
                                          alt_index = i)
              try:
                  self.variant_covgs[allele_name].append(tv)
              except KeyError:
                  self.variant_covgs[allele_name] = [tv]










