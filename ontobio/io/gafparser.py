"""
Parsers for GAF and various Association TSVs.

All parser objects instantiate a subclass of the abstract `AssocParser` object

"""
import re
import requests
import tempfile
from contextlib import closing
import subprocess
import logging

TAXON = 'TAXON'
ENTITY = 'ENTITY'
ANNOTATION = 'ANNOTATION'

class AssocParserConfig():
    """
    Configuration for an association parser
    """
    def __init__(self,
                 remove_double_prefixes=False,
                 class_map=None,
                 entity_map=None,
                 valid_taxa=None,
                 class_idspaces=None):
        self.remove_double_prefixes=remove_double_prefixes
        self.class_map=class_map
        self.entity_map=entity_map
        self.valid_taxa=valid_taxa
        self.class_idspaces=class_idspaces

class Report():
    """
    A report object that is generated as a result of a parse
    """

    # Levels
    FATAL = 'FATAL'
    ERROR = 'ERROR'
    WARNING = 'WARNING'
    
    # Warnings: TODO link to gorules
    INVALID_ID = "Invalid identifier"
    INVALID_IDSPACE = "Invalid identifier prefix"
    INVALID_TAXON = "Invalid taxon"

    """
    3 warning levels
    """
    LEVELS = [FATAL, ERROR, WARNING]
    
    def __init__(self):
        self.messages = []
        self.n_lines = 0
        self.n_assocs = 0
        self.skipped = []
        self.subjects = set()
        self.objects = set()
        self.taxa = set()
        self.references = set()

    def error(self, line, type, obj, msg=""):
        self.message(self.ERROR, line, type, obj, msg)
    def message(self, level, line, type, obj, msg=""):
        self.messages.append({'level':level,
                              'line':line,
                              'type':type,
                              'message':msg,
                              'obj':obj})

    def to_report_json(self):
        """
        Generate a summary in json format
        """

        N = 10
        report = dict(
            summary = dict(association_count = self.n_assocs,
                           line_count = self.n_lines,
                           skipped_line_count = len(self.skipped)),
            aggregate_statistics = dict(subject_count=len(self.subjects),
                                        object_count=len(self.objects),
                                        taxon_count=len(self.taxa),
                                        reference_count=len(self.references),
                                        taxon_sample=list(self.taxa)[0:N],
                                        subject_sample=list(self.subjects)[0:N],
                                        object_sample=list(self.objects)[0:N])
        )

        # grouped messages
        gm = {}
        for level in self.LEVELS:
            gm[level] = []
        for m in self.messages:
            level = m['level']
            gm[level].append(m)

        mgroups = []
        for level in self.LEVELS:
            msgs = gm[level]
            mgroup = dict(level=level,
                          count=len(msgs),
                          messages=msgs)
            mgroups.append(mgroup)
        report['groups'] = mgroups
        return report

    def to_markdown(self):
        """
        Generate a summary in markdown format
        """
        json = self.to_report_json()
        summary = json['summary']
        
        s = ""
        s = s + "\n## SUMMARY\n\n";

        s = s + " * Associations: {}\n" . format(summary['association_count'])
        s = s + " * Lines in file (incl headers): {}\n" . format(summary['line_count'])
        s = s + " * Lines skipped: {}\n" . format(summary['skipped_line_count'])
                
        stats = json['aggregate_statistics']
        s = s + "\n## STATISTICS\n\n";
        for k,v in stats.items():
            s = s + " * {}: {}\n" . format(k,v)

                

        s = s + "\n## MESSAGES\n\n";
        for g in json['groups']:
            s = s + " * {}: {}\n".format(g['level'], g['count'])
        s = s + "\n\n";
        for g in json['groups']:
            level = g['level']
            msgs = g['messages']
            if len(msgs) > 0:
                s = s + "### {}\n\n".format(level)
                for m in msgs:
                    s = s + " * {} {} `{}`\n".format(m['type'],m['message'],m['line'])
        return s
        
class AssocParser():
    """
    Abstract superclass of all association parser classes
    """

    def parse(self, file, outfile=None):
        """
        Parse a file.
        
        Arguments
        ---------

        - file : http URL, filename or `file-like-object`, for input assoc file

        - outfile : a `file-like-object`. if specified, file-like objects will be written here

        """
        file = self._ensure_file(file)
        assocs = []
        skipped = []
        n_lines = 0
        for line in file:
            n_lines = n_lines+1
            if line.startswith("!"):
                if outfile is not None:
                    outfile.write(line)
                continue
            line = line.strip("\n")
            line2, new_assocs  = self.parse_line(line)
            if new_assocs is None or new_assocs == []:
                logging.warn("SKIPPING: {}".format(new_assocs))
                skipped.append(line)
            else:
                for a in new_assocs:
                    rpt = self.report
                    rpt.subjects.add(a['subject']['id'])
                    rpt.objects.add(a['object']['id'])
                    rpt.references.update(a['evidence']['has_supporting_reference'])
                    if 'taxon' in a['subject']:
                        rpt.taxa.add(a['subject']['taxon']['id'])
                assocs = assocs + new_assocs
                if outfile is not None:
                    outfile.write(line2 + "\n")

        self.report.skipped = self.report.skipped + skipped
        self.report.n_lines = self.report.n_lines + n_lines
        self.report.n_assocs = self.report.n_assocs + len(assocs)
        logging.info("Parsed {} assocs from {} lines. Skipped: {}".
                     format(len(assocs),
                            n_lines,
                            len(skipped)))
        file.close()
        return assocs

    def skim(self, file):
        """
        Lightweight parse of a file into tuples.
        
        Note this discards metadata such as evidence.

        Return a list of tuples (subject_id, subject_label, object_id)
        """
        pass
    
    # split an ID/CURIE into prefix and local parts
    # (not currently used)
    def _parse_id(self, id):
        toks = id.split(":")
        if len(toks) == 2:
            return (toks[0],toks[1])
        else:
            return (toks[0],toks[1:].join(":"))

    # split an ID/CURIE into prefix and local parts
    def _get_id_prefix(self, id):
        toks = id.split(":")
        return toks[0]
        
    def _validate_taxon(self, taxon, line):
        if self.config.valid_taxa is None:
            return True
        else:
            if taxon in self.config.valid_taxa:
                return True
            else:
                self.report.error(line, Report.INVALID_TAXON, taxon)
                return False
        
    def _validate_id(self, id, line, context=None):
        if id.find(" ") > -1:
            self.report.error(line, Report.INVALID_ID, id)
            return False
        if id.find("|") > -1:
            # non-fatal
            self.report.error('', Report.INVALID_ID, id)
        idspace = self._get_id_prefix(id)
        if context == ANNOTATION and self.config.class_idspaces is not None:
            if idspace not in self.config.class_idspaces:
                self.report.error(line, Report.INVALID_IDSPACE, id, "allowed: {}".format(self.config.class_idspaces))
                return False
        return True

    def _split_pipe(self, v):
        if v == "":
            return []
        ids = v.split("|")
        ids = [id for id in ids if self._validate_id(id, '')]
        return ids
    
    def _pair_to_id(self, db, localid):
        if self.config.remove_double_prefixes:
            ## Switch MGI:MGI:n to MGI:n
            if localid.startswith(db+":"):
                localid = localid.replace(db+":","")
        return db + ":" + localid

    def _taxon_id(self,id):
         id = id.replace('taxon','NCBITaxon')
         self._validate_id(id,'',TAXON)
         return id
    
    def _ensure_file(self, file):
        if isinstance(file,str):
            if file.startswith("ftp"):
                f = tempfile.NamedTemporaryFile()
                fn = f.name
                cmd = ['wget',file,'-O',fn]
                subprocess.run(cmd, check=True)
                return open(fn,"r")
            elif file.startswith("http")
                url = file
                with closing(requests.get(url, stream=False)) as resp:
                    logging.info("URL: {} STATUS: {} ".format(url, resp.status_code))
                    ok = resp.status_code == 200
                    if ok:
                        return io.StringIO(resp.text)
                    else:
                        return None
            else:
                 return open("myfile.txt", "r")
        else:
            return file
            
    
    def _parse_class_expression(self, x):
        ## E.g. exists_during(GO:0000753)
        ## Atomic class expressions only
        [(p,v)] = re.findall('(.*)\((.*)\)',x)
        return {
            'property':p,
            'filler':v
        }
        
    
class GpadParser(AssocParser):
    """
    Parser for GO GPAD Format

    https://github.com/geneontology/go-annotation/blob/master/specs/gpad-gpi-1_2.md
    """

    def __init__(self,config=AssocParserConfig()):
        """
        Arguments:
        ---------

        config : a AssocParserConfig object
        """
        self.config = config
        self.report = Report()
        
    def skim(self, file):
        file = self._ensure_file(file)
        tuples = []
        for line in file:
            if line.startswith("!"):
                continue
            vals = line.split("\t")
            if len(vals) != 12:
                logging.error("Unexpected number of columns: {}. GPAD should have 12.".format(vals))
            rel = vals[2]
            # TODO: not
            id = self._pair_to_id(vals[0], vals[1])
            if not self._validate_id(id, line, ENTITY):
                continue
            t = vals[3]
            tuples.append( (id,None,t) )
        return tuples

    def parse_line(self, line):            
        """
        Parses a single line of a GPAD
        """
        vals = line.split("\t")
        [db,
         db_object_id,
         relation,
         goid,
         reference,
         evidence,
         withfrom,
         interacting_taxon_id, # TODO
         date,
         assigned_by,
         annotation_xp,
         annotation_properties] = vals

        id = self._pair_to_id(db, db_object_id)
        if not self._validate_id(id, line, ENTITY):
            return line, []
        
        if not self._validate_id(goid, line, ANNOTATION):
            return line, []
        
        assocs = []
        xp_ors = annotation_xp.split("|")
        for xp_or in xp_ors:
            xp_ands = xp_or.split(",")
            extns = []
            for xp_and in xp_ands:
                if xp_and != "":
                    extns.append(self._parse_class_expression(xp_and))
            assoc = {
                'source_line': line,
                'subject': {
                    'id':id
                },
                'object': {
                    'id':goid,
                    'extensions': extns
                },
                'relation': {
                    'id': relation
                },
                'evidence': {
                    'type': evidence,
                    'with_support_from': self._split_pipe(withfrom),
                    'has_supporting_reference': self._split_pipe(reference)
                },
                'provided_by': assigned_by,
                'date': date,
                
            }
            assocs.append(assoc)
        return line, assocs
    
class GafParser(AssocParser):
    """
    Parser for GO GAF format
    """
    
    def __init__(self,config=AssocParserConfig()):
        """
        Arguments:
        ---------

        config : a AssocParserConfig object
        """
        self.config = config
        self.report = Report()
        
    def skim(self, file):
        file = self._ensure_file(file)
        tuples = []
        for line in file:
            if line.startswith("!"):
                continue
            vals = line.split("\t")
            if len(vals) < 15:
                logging.error("Unexpected number of vals: {}. GAFv1 has 15, GAFv2 has 17.".format(vals))

            if vals[3] != "":
                continue
            id = self._pair_to_id(vals[0], vals[1])
            if not self._validate_id(id, line, ENTITY):
                continue
            n = vals[2]
            t = vals[4]
            tuples.append( (id,n,t) )
        return tuples


    def parse_line(self, line, class_map=None, entity_map=None):
        """
        Parses a single line of a GAF
        """
        config = self.config
        
        vals = line.split("\t")
        # GAF v1 is defined as 15 cols, GAF v2 as 17.
        # We treat everything as GAF2 by adding two blank columns.
        # TODO: check header metadata to see if columns corresponds to declared dataformat version
        if len(vals) == 15:
            vals = vals + ["",""]
        [db,
         db_object_id,
         db_object_symbol,
         qualifier,
         goid,
         reference,
         evidence,
         withfrom,
         aspect,
         db_object_name,
         db_object_synonym,
         db_object_type,
         taxon,
         date,
         assigned_by,
         annotation_xp,
         gene_product_isoform] = vals

        ## --
        ## db + db_object_id. CARD=1
        ## --
        id = self._pair_to_id(db, db_object_id)
        if not self._validate_id(id, line, ENTITY):
            return line, []
        
        if not self._validate_id(goid, line, ANNOTATION):
            return line, []
        
        ## --
        ## optionally map goid and entity (gp) id
        ## --
        # Example use case: map2slim
        if config.class_map is not None:
            goid = self.map_id(goid, config.class_map)
            if not self._validate_id(goid, line, ANNOTATION):
                return line, []
            vals[4] = goid
            
        # Example use case: mapping from UniProtKB to MOD ID
        if config.entity_map is not None:
            id = self.map_id(id, config.entity_map)
            toks = id.split(":")
            db = toks[0]
            db_object_id = toks[1:]
            vals[1] = db_object_id

        ## --
        ## end of line re-processing
        ## --
        # regenerate line post-mapping
        line = "\t".join(vals)

        ## --
        ## taxon CARD={1,2}
        ## --
        ## if a second value is specified, this is the interacting taxon
        taxa = [self._taxon_id(x) for x in taxon.split("|")]
        taxon = taxa[0]
        in_taxa = taxa[1:]
        self._validate_taxon(taxon, line)
        
        ## --
        ## db_object_synonym CARD=0..*
        ## --
        synonyms = db_object_synonym.split("|")
        if db_object_synonym == "":
            synonyms = []

        ## --
        ## process associations
        ## --
        ## note that any disjunctions in the annotation extension
        ## will result in the generation of multiple associations
        assocs = []
        xp_ors = annotation_xp.split("|")
        for xp_or in xp_ors:

            # gather conjunctive expressions in extensions field
            xp_ands = xp_or.split(",")
            extns = []
            for xp_and in xp_ands:
                if xp_and != "":
                    extns.append(self._parse_class_expression(xp_and))

            ## --
            ## qualifier
            ## --
            ## we generate both qualifier and relation field
            relation = None
            qualifiers = qualifier.split("|")
            if qualifier == '':
                qualifiers = []
            negated =  'NOT' in qualifiers
            other_qualifiers = [q for q in qualifiers if q != 'NOT']

            ## In GAFs, relation is overloaded into qualifier.
            ## If no explicit non-NOT qualifier is specified, use
            ## a default based on GPI spec
            if len(other_qualifiers) > 0:
                relation = other_qualifiers[0]
            else:
                if aspect == 'C':
                    relation = 'part_of'
                elif aspect == 'P':
                    relation = 'involved_in'
                elif aspect == 'F':
                    relation = 'enables'
                else:
                    relation = None

            ## --
            ## goid
            ## --
            object = {'id':goid,
                      'taxon': taxon}

            # construct subject dict
            subject = {
                'id':id,
                'label':db_object_symbol,
                'type': db_object_type,
                'fullname': db_object_name,
                'synonyms': synonyms,
                'taxon': {
                    'id': taxon
                }
            }
            
            ## --
            ## gene_product_isoform
            ## --
            ## This is mapped to a more generic concept of subject_extensions
            subject_extns = []
            if gene_product_isoform is not None and gene_product_isoform != '':
                subject_extns.append({'property':'isoform', 'filler':gene_product_isoform})

            ## --
            ## evidence
            ## reference
            ## withfrom
            ## --
            evidence = {
                'type': evidence,
                'has_supporting_reference': self._split_pipe(reference)
            }
            evidence['with_support_from'] = self._split_pipe(withfrom)

            ## Construct main return dict
            assoc = {
                'source_line': line,
                'subject': subject,
                'object': object,
                'negated': negated,
                'qualifiers': qualifiers,
                'relation': {
                    'id': relation
                },
                'evidence': evidence,
                'provided_by': assigned_by,
                'date': date,
                
            }
            if len(subject_extns) > 0:
                assoc['subject_extensions'] = subject_extns
            if len(extns) > 0:
                assoc['object_extensions'] = extns
                
            assocs.append(assoc)
        return line, assocs
    
class HpoaParser(GafParser):
    """
    Parser for HPOA format

    http://human-phenotype-ontology.github.io/documentation.html#annot

    Note that there are similarities with Gaf format, so we inherit from GafParser, and override
    """
    
    def __init__(self,config=AssocParserConfig()):
        """
        Arguments:
        ---------

        config : a AssocParserConfig object
        """
        self.config = config
        self.report = Report()

    def parse_line(self, line, class_map=None, entity_map=None):
        """
        Parses a single line of a HPOA
        """
        config = self.config

        # http://human-phenotype-ontology.github.io/documentation.html#annot
        vals = line.split("\t")
        [db,
         db_object_id,
         db_object_symbol,
         qualifier,
         hpoid,
         reference,
         evidence,
         onset,
         frequency,
         withfrom,
         aspect,
         db_object_synonym,
         date,
         assigned_by] = vals

        # hardcode this, as HPOA is currently human-only
        taxon = 'NCBITaxon:9606'

        # hardcode this, as HPOA is currently disease-only
        db_object_type = 'disease'
        
        ## --
        ## db + db_object_id. CARD=1
        ## --
        id = self._pair_to_id(db, db_object_id)
        if not self._validate_id(id, line, ENTITY):
            return line, []
        
        if not self._validate_id(hpoid, line, ANNOTATION):
            return line, []
        
        ## --
        ## optionally map hpoid and entity (disease) id
        ## --
        # Example use case: HPO map2slim
        if config.class_map is not None:
            hpoid = self.map_id(hpoid, config.class_map)
            if not self._validate_id(hpoid, line, ANNOTATION):
                return line, []
            vals[4] = hpoid
            
        # Example use case: mapping from OMIM to Orphanet
        if config.entity_map is not None:
            id = self.map_id(id, config.entity_map)
            toks = id.split(":")
            db = toks[0]
            db_object_id = toks[1:]
            vals[1] = db_object_id

        ## --
        ## end of line re-processing
        ## --
        # regenerate line post-mapping
        line = "\t".join(vals)
        
        ## --
        ## db_object_synonym CARD=0..*
        ## --
        synonyms = db_object_synonym.split("|")
        if db_object_synonym == "":
            synonyms = []


        ## --
        ## qualifier
        ## --
        ## we generate both qualifier and relation field
        relation = None
        qualifiers = qualifier.split("|")
        if qualifier == '':
            qualifiers = []
        negated =  'NOT' in qualifiers
        other_qualifiers = [q for q in qualifiers if q != 'NOT']

        ## CURRENTLY NOT USED
        if len(other_qualifiers) > 0:
            relation = other_qualifiers[0]
        else:
            if aspect == 'O':
                relation = 'has_phenotype'
            elif aspect == 'I':
                relation = 'has_inheritance'
            elif aspect == 'M':
                relation = 'mortality'
            elif aspect == 'C':
                relation = 'has_onset'
            else:
                relation = None

        ## --
        ## hpoid
        ## --
        object = {'id':hpoid,
                  'taxon': taxon}

        # construct subject dict
        subject = {
            'id':id,
            'label':db_object_symbol,
            'type': db_object_type,
            'synonyms': synonyms,
            'taxon': {
                'id': taxon
            }
        }

        ## --
        ## evidence
        ## reference
        ## withfrom
        ## --
        evidence = {
            'type': evidence,
            'has_supporting_reference': self._split_pipe(reference)
        }
        evidence['with_support_from'] = self._split_pipe(withfrom)

        ## Construct main return dict
        assoc = {
            'source_line': line,
            'subject': subject,
            'object': object,
            'negated': negated,
            'qualifiers': qualifiers,
            'relation': {
                'id': relation
            },
            'evidence': evidence,
            'provided_by': assigned_by,
            'date': date,
            
        }
            
        return line, [assoc]
