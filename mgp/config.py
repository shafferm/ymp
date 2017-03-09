import csv
import os
import re
import sys
import inspect
from functools import lru_cache
from contextlib import contextmanager

from snakemake.io import expand, get_wildcard_names, apply_wildcards
from snakemake.workflow import config
from snakemake.utils import format

from mgp.snakemake import ExpandableWorkflow, ColonExpander


#TODO: allow comment lines starting with # or ; or somesuch
@contextmanager
def AutoDictReader(filename):
    """Open a CSV style document"""
    with open(filename) as f:
        # sniff CSV type
        dialect = csv.Sniffer().sniff(f.read(10240))
        f.seek(0)
        reader = csv.DictReader(f, dialect=dialect)
        yield reader


class DatasetConfig(object):
    """Contains configuration for a source dataset to be processed"""
    def __init__(self, cfg):
        self.cfg = cfg
        self.file = self.cfg['file']
        self.fieldnames = None
        self._runs = None
        self._props = {}

    def loadRuns(self, id_col):
        with AutoDictReader(self.file) as reader:
            self.fieldnames = reader.fieldnames
            self._runs = {row[id_col]:row for row in reader}
        self.loadExtra()
        self.clean_uninformative()
        
    def loadExtra(self):
        if 'extra_file' in self.cfg:
            keyname = self.cfg['extra_name_col']
            only_extra=set()
            matched=set()
            
            with AutoDictReader(self.cfg['extra_file']) as reader:
                self.fieldnames += reader.fieldnames
                for row in reader:
                    try:
                        self._runs[row[keyname]].update(row)
                        matched.add(row[keyname])
                    except KeyError as e:
                        only_extra.add(row[keyname])

            only_main=set([run for run in self._runs if run not in matched])

            if len(only_main) > 0:
                raise Exception("Need to deal with incomplete extra data: FIXME")


    def clean_uninformative(self):
        common = {}
        for field in self.fieldnames:
            l = len(set([self._runs[run][field] for run in self._runs]))
            if not l:
                raise Exception("Column with no values? Something went wrong!")
            if l == 1:
                common[field] = self._runs[next(iter(self._runs))][field]

        for run in self._runs:
            for field in common:
                del self._runs[run][field]

        self.fieldnames = [field for field in self.fieldnames if field not in common]
        
                
    def print_runs(self):
        w = csv.writer(sys.stdout)
        w.writerow(self.fieldnames)
        w.writerows([ [self._runs[run][field] for field in self.fieldnames]
                      for run in self._runs])

                
    @property
    def runs(self):
        return self._runs

    @property
    def props(self):
        return self.fieldnames

    class CantLoad(Exception):
        pass

    def get_context(self, wc):
        return self.Context(self, wc)

    class Context(object):
        def __init__(self, dc, wc):
            self.dc = dc
            self.wc = wc
            self.colname = dc.name_col
            dirname = ""
            try:
                dirname = wc.dir#.split(".")
            except AttributeError:
                pass
            
            regex = r"\.by_({})(?:[./]|$)".format("|".join(dc.fieldnames))
            re.compile(regex)
            groups = re.findall(regex, dirname)
            if len(groups) > 0:
                self.colname=groups[-1]

            self.byname = dc.name_col
            if hasattr(wc, 'by'):
                groups = re.findall(regex, wc.by)
                if len(groups) > 0:
                    self.byname = groups[-1]

        @property
        def targets(self):
            if self.colname:
                return set([self.dc.runs[run][self.colname]
                            for run in self.dc.runs])
            else:
                return self.dc.runs

        @property
        def sources(self):
            sources = set([self.dc.runs[run][self.colname]
                           for run in self.dc.runs
                           if self.dc.runs[run][self.byname] == self.wc.target])
            #print(sources)
            return sources
            
        

class SraRunTable(DatasetConfig):
    """Contains dataset configuration specified as a SraRunTable"""
    def __init__(self, cfg):
        super().__init__(cfg)
        if self.cfg['type'] != 'SraRunTable':
            raise self.CantLoad()
        if 'name_col' in self.cfg:
            self.name_col = self.cfg['name_col']
        else:
            self.name_col = "Libary_Name_s"
        
        self.loadRuns(self.name_col)

    def FQpath(self, run, pair):
        return os.path.join(
            icfg.scratchdir,"SRR",
            "{}_{}.fastq.gz".format(self.runs[run]['Run_s'], pair+1)
        )

    @property
    def fastq_basenames(self):
        return ["{}.{}".format(run, icfg.pairnames[pair])
                for run in self.runs
                for pair in range(2)]
    

class Mapfile(DatasetConfig):
    """Contains a dataset configuration specified as a CSV"""
    def __init__(self, cfg):
        super().__init__(cfg)
        if self.cfg['type'] != 'CSV':
            raise self.CantLoad()
        
        self.basedir=os.path.dirname(self.file)
        self.fq_cols = self.cfg['fq_cols']
        self.name_col = self.cfg['name_col']

        self.loadRuns(self.name_col)

    def FQpath(self, run, pair):
        return os.path.join(self.basedir, self.runs[run][self.fq_cols[pair]])

    @property
    def fastq_basenames(self):
        return ["{}.{}".format(run, icfg.pairnames[pair])
                for run in self.runs
                for pair in range(len(self.fq_cols))]


class ConfigExpander(ColonExpander):
    def __init__(self, config_mgr):
        super().__init__()
        self.config_mgr = config_mgr

    class Formatter(ColonExpander.Formatter):
        def _get_column(self, dirname):
            regex = r"\.by_({})(?:[./]|$)".format("|".join(colnames))
            groups = re.findall(regex, dirname)
            if len(groups) == 0:
                return None
            else:
                return groups[-1]
            
        def _get_targets(self, data):
            return set([
                data[item][colname]
                for item in data
                ])

        def get_value(self, field_name, args, kwargs):
            #print("get_value: {} {} {}".format( field_name, args, kwargs))

            # try to resolve variable as property of the config_mgr
            try:
                return getattr(self.expander.config_mgr, field_name)
            except AttributeError:
                pass

            try:
                ds = self.expander.config_mgr.getDatasetFromDir(kwargs['wc'].dir)
                return getattr(ds, field_name)
            except AttributeError:
                pass

            try:
                ct = ds.get_context(kwargs['wc'])
                return getattr(ct, field_name)
            except AttributeError as e:
                pass

            return super().get_value(field_name, args, kwargs)
        

class ConfigMgr(object):
    """Interface to configuration. Singleton as "icfg" """
    
    def __init__(self):
        self._datasets = {}
        from snakemake.workflow import config
        self._config = config
        self.config_expander = ConfigExpander(self)

        
    def init(self):
        self._datasets = { cfg: self.loadcfg(cfg)
                          for cfg in self._config['mapfiles'] }

    def loadcfg(self, cfg):
        for sc in DatasetConfig.__subclasses__():
            try:
                return sc(self._config['mapfiles'][cfg])
            except DatasetConfig.CantLoad as e:
                pass
        raise Exception("Unable to parse configuration for {}".format(cfg))

    def __len__(self):
        "Our length is the number of datasets"
        return length(self._datasets)

    def __getitem__(self, key):
        "Returns DatasetConfig"
        return self._datasets[key]

    def __iter__(self, key):
        "Returns iterator over DatasetConfigs"
        return iter(self._datasets)

    @property
    def pairnames(self):
        return self._config['pairnames']

    @property
    def scratchdir(self):
        try:
            return self._config['directories']['scratch']
        except:
            raise KeyError("Missing directories/scratch in config")

    @property
    def scratch(self):
        return self.scratchdir

    @property
    def reportsdir(self):
        try:
            return self._config['directories']['reports']
        except:
            raise KeyError("Missing directories/reports in config")

    @property
    def sra(self):
        try:
            return self._config['directories']['sra']
        except:
            raise KeyError("Missing directories/reports in config")
    
            
    @property
    def datasets(self):
        """Returns list of all configured datasets"""
        return self._datasets.keys()

    @property
    def allruns(self):
        return self.getRuns()

    @property
    def allprops(self):
        return self.getProps()

    def getDatasetFromDir(self, dirname):
        try:
            ds = dirname.split(".", 1)[0]
            return self._datasets[ds]
        except:
            raise KeyError("no dataset found matching '{}'".format(dirname))
        
    def expand(self, template):
        return lambda wc: self._expand(template, wc)

    def _expand(self, template, wc={}):
        if isinstance(template, str):
            template = [template]
        names = set()
        for item in template:
            names |= get_wildcard_names(item)
 
        sources = [wc]
        try:
            ds = self.getDatasetFromDir(wc.dir)
            sources += [ds]
        except:
            pass
        sources += [self]

        fields = {}
        for name in names:
            for source in sources:
                if name in dir(source):
                    fields[name] = getattr(source, name)
                    break
            if not name in fields:
                fields[name] = "{{{}}}".format(name)

        res = expand(template, **fields)
        return res
            
    def FQpath(self, dataset, run, pairsuff):
        return self._datasets[dataset].FQpath(run, self.pairnames.index(pairsuff))


    def getRuns(self, datasets=None):
        """Returns list of names of Runs of `dataset`, or names of all configured Runs"""
        if not datasets:
            datasets = self.datasets
        if isinstance(datasets, str):
            datasets = [datasets]
        return [
            run             
            for dataset in datasets
            for run in self._datasets[dataset].runs
        ]

    def getProps(self, datasets=None):
        """Returns list of properties of `dataset` runs"""
        if not datasets:
            datasets = self.datasets
        if isinstance(datasets, str):
            datasets = [datasets]
        return [
            prop
            for dataset in datasets
            for prop in self._datasets[dataset].props
        ]
    

icfg = ConfigMgr()