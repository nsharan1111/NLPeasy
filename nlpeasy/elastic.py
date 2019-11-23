# -*- coding: utf-8 -*-

"""Main module."""


import pandas as pd
from typing import Optional
import elasticsearch
from . import kibana
from . import docker

from .util import Progbar, chunker, print_or_display, rmNanFromDict


def connect_elastic(dockerPrefix: str ='nlp', startOnDocker: bool = True,
            host: str = 'localhost', elasticPort: Optional[int] = None, kibanaPort: Optional[int] = None, kibanaHost: str = None,
            elkVersion: str = '7.1.1', mountVolumePrefix: Optional[str] = None,
            verbose: bool = True, failOnNotAvailable: bool = False, **kwargs) -> "ElasticStack":
    """Connect to running Elasticsearch and Kibana servers or start one on Docker.

    First this will try to connect to the specified host/ports.
    If no server can be reached then the docker is explored whether containers with name
    `{dockerPrefix}_elastic` and `{dockerPrefix}_kibana` are running and if found they are used.
    Else, such containers will be started.

    Parameters
    ----------
    dockerPrefix :
        Docker containers for Elasticsearch and Kibana, and the docker network will be prefixed
        with this + '_' as is customary e.g. in docker-compose
    startOnDocker :
        If there is no reachable Elasticsearch server, should one be started on Docker (default: True)
    host :
        The host to try to connect to (default: 'localhost')
    elasticPort :
        The port on which try to connect to or start Elasticsearch on if not yet started.
        If ``None`` (default) then docker will find a port and the returned ELK will be using that.
    kibanaPort :
        The port on which try to connect to or start Kibana on if not yet started.
        If ``None`` (default) then docker will find a port and the returned ELK will be using that.
    kibanaHost :
        The host to try to connect to for Kibana.
        If ``None`` (default) the same as ``host``.
    elkVersion :
        The version of the Elastic Stack to download if starting on Docker.
    mountVolumePrefix :
        If a docker container will be started this specifies where in the filesystem of the host
        the data should be saved. If ``None`` (default) data is not saved and will not survive restarts of the container.
    verbose :
        Should information be printed out.
    failOnNotAvailable :
        Should there be an error raised.
    kwargs :
        Passed to :meth:`~nlpeasy.docker.start_elastic_on_docker`

    Returns
    -------
    ElasticStack
        The elastic stack found.
    """
    kibanaHost = kibanaHost or host
    elasticPort = elasticPort or 9200
    kibanaPort = kibanaPort or 5601
    log = print_or_display if verbose else lambda x: None
    elk = ElasticStack(host=host, elasticPort=elasticPort, kibanaPort=kibanaPort, kibanaHost=kibanaHost)
    if elk.alive():
        log(f"Elasticsearch already running")
        # TODO warn if version mismatches elkVersion param
        log(elk)
        return elk
    if dockerPrefix is None:
        if failOnNotAvailable:
            raise RuntimeError(f"No running elasticsearch found on {host}:{elasticPort}.")
        else:
            log(f"No running elasticsearch found on {host}:{elasticPort}.")
            return None

    # Let's pass it on to docker:
    log(f"No elasticsearch on {host}:{elasticPort} found, trying connect to docker container with prefix {dockerPrefix}")
    elk = docker.elasticStackFromDocker(containerPrefix=dockerPrefix, setAsDefaultStack=False)
    if elk is None or not elk.alive():
        if startOnDocker:
            log(f"No docker container with prefix {dockerPrefix}; starting one")
            assert all(i not in kwargs for i in ['mountVolumePrefix','version','prefix'])
            elk = docker.start_elastic_on_docker(prefix=dockerPrefix, elkVersion=elkVersion, mountVolumePrefix=mountVolumePrefix, **kwargs)
        else:
            msg = f"No running elasticsearch found on docker with prefix {dockerPrefix}."
            if failOnNotAvailable:
                raise RuntimeError(msg)
            else:
                log(msg)
                return None
    log(elk)
    return elk

class ElasticStack(object):
    def __init__(self, host='localhost', elasticPort=9200, kibanaPort=5601, protocol='http',
                kibanaHost=None, kibanaProtocol=None, verify_certs=True, setAsDefaultStack=True, **kwargs):
        self._host = host
        self._elasticPort = elasticPort
        self._protocol = protocol
        self._verify_certs = verify_certs

        self._es = None
        self._kibana = None
        self._elasticKwargs = kwargs

        self.kibana = kibana.Kibana(
            host=self._host if kibanaHost is None else kibanaHost,
            port=kibanaPort,
            protocol=self._protocol if kibanaProtocol is None else kibanaProtocol,
            verify_certs=self._verify_certs
        )

        if setAsDefaultStack:
            setDefaultStack(self)

    def alive(self, verbose=True):
        import logging
        urllib_logger = logging.getLogger("request")
        orig_max_retries = 3
        orig_level = urllib_logger.level
        result = False
        try:
            # BUG Disabling logging does not work:
            urllib_logger.setLevel(logging.FATAL)
            orig_max_retries = self.es.transport.max_retries
            self.es.transport.max_retries = 0
            result = self.es.ping() and self.kibana.alive()
        except Exception as e:
            if verbose:
                print(e)
        self.es.transport.max_retries = orig_max_retries
        urllib_logger.setLevel(orig_level)
        return result

    def waitFor(self, timeout: float=10, interval: float=0.5, raise_error=False, verbose=False) -> bool:
        from datetime import datetime
        from time import sleep
        start = datetime.now()
        while timeout <= 0 or (datetime.now() - start).seconds < timeout:
            if self.alive(verbose=verbose):
                return True
            sleep(interval)
        if raise_error:
            raise RuntimeError("")
        return False

    def url(self):
        return f"{self._protocol}://{self._host}:{self._elasticPort}"
    def __repr__(self):
        return f"ElasticSearch on {self.url()}\n" + self.kibana.__repr__()
    def _repr_html_(self):
        return f"ElasticSearch on <a href='{self.url()}'>{self.url()}</a> <br> " + self.kibana._repr_html_()


    @property
    def es(self):
        if self._es is None:
            host = { 'host': self._host, 'port': self._elasticPort, 'use_ssl': self._protocol == 'https',  }
            self._es = elasticsearch.Elasticsearch([host], verify_certs=self._verify_certs, **self._elasticKwargs)
        return self._es

    def getAnalysis(self, lang='english', synonyms=None):
        filter_names = []
        if lang == 'english':
            filter_names.append("english_possessive_stemmer")
        filter_names.append('lowercase')
        filters = {
            f"{lang}_stop": {
                "type": "stop",
                "stopwords": f"_{lang}_"
            },
            f"{lang}_stemmer": {
                "type": "stemmer",
                "language": f"{lang}"
            },
        }
        filter_names.extend(filters.keys())
        if lang == 'english':
            filters["english_possessive_stemmer"] = {
                "type": "stemmer",
                "language": "possessive_english"
            }
        if synonyms is not None:
            filters[f"{lang}_synonym"] = {
                "type": "synonym",
                "synonyms": synonyms
                # "synonyms_path": "analysis/synonym.txt"
            }
            filter_names.append(f"{lang}_synonym")

        analyzer = {
                    f"{lang}_syn": {
                                "tokenizer": "standard",
                                "filter": filter_names
                            }

        }
        return filters, analyzer


    # TODO languages, synonyms,
    def createIndex(self, index='texts',doctype='_doc',create=True,
            textCols=[], tagCols=[], geoPointCols = [], synonyms=[], dateCol=None, lang='english',
            deleteOld=True, verbose=False):
        # assert lang == 'english'
        properties = {}
        for k in textCols:
            # TODO Make sure that the analyzer is created as f"{lang}_syn":
            properties[k] =  { "type": "text", "fielddata": True, "analyzer": f"{lang}_syn" }
        for k in tagCols:
            properties[k] =  { "type": "keyword" }
        for k in geoPointCols:
            properties[k] =  { "type": "geo_point" }
        properties["suggest"] = { "type" : "completion" }
        mapping = {
            #"_timestamp": {"enabled": "false"},
            "properties": properties
        }
        if self.es.info()['version']['number'] < '7':
            mapping = { doctype: mapping }
        if create:
            filters, analyzer = self.getAnalysis(lang, synonyms)
            body={
                "settings": {
                    "analysis": {
                        "filter": filters,
                        # {
                        #     "synonym": {
                        #         "type": "synonym",
                        #         "synonyms": synonyms
                        #         # "synonyms_path": "analysis/synonym.txt"
                        #     }
                        # },
                        "analyzer": analyzer,
                        # "analyzer": {
                        #     "english_syn": {
                        #         "tokenizer": "standard",
                        #         "filter": [
                        #             "english_possessive_stemmer",
                        #             "lowercase",
                        #             "english_stop",
                        #             "english_stemmer",
                        #             "synonym"
                        #         ]
                        #     }
                        # }
                    }
                },

                "mappings": mapping
            }
            if verbose:
                print(body)
            if deleteOld:
                try:
                    self.es.indices.delete(index)
                except:
                    pass
            self.es.indices.create(index=index, body=body) # , ignore=[]
            return(body)
        else:
            self.es.indices.put_mapping(index=index, doc_type=doctype, body=mapping)

    def loadDocs(self, index, texts, doctype='_doc', dateCol=None, deleteOld=False, chunksize=1000, idCol=None,
                suggestCol=None, showProgbar=True):
        if idCol is None:
            idCol = texts.index
        if deleteOld:
            try:
                self.es.indices.delete(index)
            except:
                pass
        #createIndex(index=index, create=deleteOld)

        for ic, cdf in enumerate(chunker(texts, chunksize, progbar=showProgbar)):
            docs = cdf.to_dict(orient='records')
            for ii, doc in enumerate(docs):
                i = ic * chunksize + ii
                doc = rmNanFromDict(doc)
                if suggestCol and suggestCol in doc:
                    doc['suggest'] = doc[suggestCol]
                try:
                    self.es.index(index=index, doc_type=doctype, id=idCol[i], body=doc)
                except elasticsearch.ElasticsearchException as ex:
                    print(ex)
                    print(doc)
                    print('=' * 80)

    def truncate(self, index, doctype='text'):
        self._es.delete_by_query(index, {
            "query" : {
                "match_all" : {}
            }
        })

    def show_kibana(self, how=None, *args, **kwargs):
        """Opens the Kibana UI either by opening it in the default webbrowser or by showing the URL.

        Parameters
        ----------
        how :
            One or more of ``'print'``, ``'webbrowser'``, or ``'jupyter'``
        args :
            passed to :meth:`~nlpeasy.kibana.Kibana.kibanaUrl`
        kwargs
            passed to :meth:`~nlpeasy.kibana.Kibana.kibanaUrl`

        Returns
        -------
        If ``how`` contains ``'jupyter'`` then the IPython display HTML with a link.
        """
        self.kibana.show_kibana(how=how, *args, **kwargs)

__DEFAULT_STACK = None
def defaultStack():
    if __DEFAULT_STACK is None:
        __DEFAULT_STACK = ElasticStack()
    return __DEFAULT_STACK
def setDefaultStack(es):
    global __DEFAULT_STACK
    __DEFAULT_STACK = es

