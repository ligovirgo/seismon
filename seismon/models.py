"""
Database schema.
"""

from datetime import datetime, date
import simplejson as json
import enum
import os
import glob
import time
import copy
import configparser

from astropy import table
from astropy import coordinates
from astropy import units as u
from astropy.time import Time, TimeDelta
import pkg_resources
import numpy as np
import pandas as pd

import sqlalchemy as sa
from sqlalchemy.ext.associationproxy import association_proxy
from sqlalchemy.ext.declarative import declarative_base, declared_attr
from sqlalchemy.orm import sessionmaker, scoped_session, relationship
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import JSONB
from arrow.arrow import Arrow

from obspy.geodetics.base import gps2dist_azimuth
from obspy.taup import TauPyModel

import seismon
from seismon import (eqmon, utils)


DBSession = scoped_session(sessionmaker())
EXECUTEMANY_PAGESIZE = 50000
utcnow = func.timezone('UTC', func.current_timestamp())

data_types = {
    int: 'int',
    float: 'float',
    bool: 'bool',
    dict: 'dict',
    str: 'str',
    list: 'list'
    }


class Encoder(json.JSONEncoder):
    """Extends json.JSONEncoder with additional capabilities/configurations."""
    def default(self, o):
        if isinstance(o, (datetime, Arrow, date)):
            return o.isoformat()

        elif isinstance(o, bytes):
            return o.decode('utf-8')

        elif hasattr(o, '__table__'):  # SQLAlchemy model
            return o.to_dict()

        elif o is int:
            return 'int'

        elif o is float:
            return 'float'

        elif type(o).__name__ == 'ndarray': # avoid numpy import
            return o.tolist()

        elif type(o).__name__ == 'DataFrame':  # avoid pandas import
            o.columns = o.columns.droplevel('channel')  # flatten MultiIndex
            return o.to_dict(orient='index')

        elif type(o) is type and o in data_types:
            return data_types[o]

        return json.JSONEncoder.default(self, o)

def to_json(obj):
    return json.dumps(obj, cls=Encoder, indent=2, ignore_nan=True)

class BaseMixin(object):
    query = DBSession.query_property()
    id = sa.Column(sa.Integer, primary_key=True)
    created_at = sa.Column(sa.DateTime, nullable=False, default=utcnow)
    modified = sa.Column(sa.DateTime, default=utcnow, onupdate=utcnow,
                         nullable=False)

    @declared_attr
    def __tablename__(cls):
        return cls.__name__.lower() + 's'

    __mapper_args__ = {'confirm_deleted_rows': False}

    def __str__(self):
        return to_json(self)

    def __repr__(self):
        attr_list = [f"{c.name}={getattr(self, c.name)}"
                     for c in self.__table__.columns]
        return f"<{type(self).__name__}({', '.join(attr_list)})>"

    def to_dict(self):
        if sa.inspection.inspect(self).expired:
            DBSession().refresh(self)
        return {k: v for k, v in self.__dict__.items() if not k.startswith('_')}

    @classmethod
    def get_if_owned_by(cls, ident, user, options=[]):
        obj = cls.query.options(options).get(ident)

        if obj is not None and not obj.is_owned_by(user):
            raise AccessError('Insufficient permissions.')

        return obj

    def is_owned_by(self, user):
        raise NotImplementedError("Ownership logic is application-specific")

    @classmethod
    def create_or_get(cls, id):
        obj = cls.query.get(id)
        if obj is not None:
            return obj
        else:
            return cls(id=id)

Base = declarative_base(cls=BaseMixin)

# The db has to be initialized later; this is done by the app itself
# See `app_server.py`
def init_db(user, database, password=None, host=None, port=None):
    url = 'postgresql://{}:{}@{}:{}/{}'
    url = url.format(user, password or '', host or '', port or '', database)

    conn = sa.create_engine(url, client_encoding='utf8')
#                            executemany_mode='values',
#                            executemany_values_page_size=EXECUTEMANY_PAGESIZE)

    DBSession.configure(bind=conn)
    Base.metadata.bind = conn

    return conn


class Earthquake(Base):
    """Earthquake information"""

    event_id = sa.Column(
        sa.String,
        unique=True,
        nullable=False,
        comment='Earthquake ID')

    lat = sa.Column(
        sa.Float,
        nullable=False,
        comment='Latitude')

    lon = sa.Column(
        sa.Float,
        nullable=False,
        comment='Longitude')

    depth = sa.Column(
        sa.Float,
        nullable=False,
        comment='Depth')

    magnitude = sa.Column(
        sa.Float,
        nullable=False,
        comment='Magnitude',
        index=True)

    date = sa.Column(
        sa.DateTime,
        nullable=False,
        comment='UTC event timestamp',
        index=True)

    sent = sa.Column(
        sa.DateTime,
        nullable=False,
        comment='UTC sent timestamp')

    predictions = relationship(lambda: Prediction)


class Ifo(Base):
    """Detector information"""

    id = sa.Column(sa.Integer, primary_key=True)

    ifo = sa.Column(
        sa.String,
        unique=True,
        nullable=False,
        comment='Detector name')

    lat = sa.Column(
        sa.Float,
        nullable=False,
        comment='Latitude')

    lon = sa.Column(
        sa.Float,
        nullable=False,
        comment='Longitude')

    predictions = relationship(lambda: Prediction)


class Prediction(Base):
    """Prediction information"""

    event_id = sa.Column(
        sa.String,
        sa.ForeignKey(Earthquake.event_id),
        nullable=False,
        comment='Earthquake ID')

    ifo = sa.Column(
        sa.String,
        sa.ForeignKey(Ifo.ifo),
        nullable=False,
        comment='Detector name',
        index=True)

    D = sa.Column(
        sa.Float,
        nullable=False,
        comment='Distance [km]')

    P = sa.Column(
        sa.DateTime,
        nullable=False,
        comment='P-wave time')

    S = sa.Column(
        sa.DateTime,
        nullable=False,
        comment='S-wave time')

    R2p0 = sa.Column(
           sa.DateTime,
           nullable=False,
           comment='R-2.0 km/s-wave time')

    R3p5 = sa.Column(
           sa.DateTime,
           nullable=False,
           comment='R-3.5 km/s-wave time')

    R5p0 = sa.Column(
           sa.DateTime,
           nullable=False,
           comment='R-5.0 km/s-wave time')

    Rfamp = sa.Column(
            sa.Float,
            nullable=False,
            comment='Earthquake amplitude predictions [m/s]')

    Lockloss = sa.Column(
               sa.INT,
               nullable=False,
               comment='Earthquake amplitude predictions [m/s]')


def compute_predictions(earthquake, ifo):

    Dist, Ptime, Stime, Rtwotime, RthreePointFivetime, Rfivetime = compute_traveltimes(earthquake, ifo) 
    Rfamp, Lockloss = compute_amplitudes(earthquake, ifo)

    DBSession().merge(Prediction(event_id=earthquake.event_id,
                                 ifo=ifo.ifo,
				 D=Dist,
                                 P=Ptime,
                                 S=Stime,
                                 R2p0=Rtwotime,
                                 R3p5=RthreePointFivetime,
                                 R5p0=Rfivetime,
                                 Rfamp=Rfamp,
                                 Lockloss=int(Lockloss)))
    print('Prediction ifo %s for event: %s' % (ifo.ifo, earthquake.event_id))
    DBSession().commit()


def compute_traveltimes(earthquake, ifo):

    seismonpath = os.path.dirname(seismon.__file__)
    scriptpath = os.path.join(seismonpath,'input')

    depth = earthquake.depth
    eqtime = Time(earthquake.date, format='datetime')
    eqlat = earthquake.lat
    eqlon = earthquake.lon
    ifolat = ifo.lat
    ifolon = ifo.lon

    distance,fwd,back = gps2dist_azimuth(eqlat,
                                         eqlon,
                                         ifolat,
                                         ifolon)
    Dist = distance/1000
    degree = (distance/6370000)*(180/np.pi)

    model = TauPyModel(model="iasp91")

    Rtwotime = eqtime+TimeDelta(distance/2000.0 * u.s)
    RthreePointFivetime = eqtime+TimeDelta(distance/3500.0 * u.s)
    Rfivetime = eqtime+TimeDelta(distance/5000.0 * u.s)

    try:
        arrivals = model.get_travel_times(source_depth_in_km=depth,
                                          distance_in_degree=degree)

        Ptime = -1
        Stime = -1
        for phase in arrivals:
            if Ptime == -1 and phase.name.lower()[0] == "p":
                Ptime = eqtime+TimeDelta(phase.time * u.s)
            if Stime == -1 and phase.name.lower()[0] == "s":
                Stime = eqtime+TimeDelta(phase.time * u.s)
    except:
        Ptime, Stime = Rtwotime, Rtwotime 

    return Dist, Ptime.datetime, Stime.datetime, Rtwotime.datetime, RthreePointFivetime.datetime, Rfivetime.datetime


def compute_amplitudes(earthquake, ifo):

    seismonpath = os.path.dirname(seismon.__file__)
    scriptpath = os.path.join(seismonpath,'input')

    depth = earthquake.depth
    eqtime = Time(earthquake.date, format='datetime')
    eqlat = earthquake.lat
    eqlon = earthquake.lon
    mag = earthquake.magnitude
    ifolat = ifo.lat
    ifolon = ifo.lon

    if ifo == "LLO":
        trainFile = os.path.join(scriptpath,'LLO_processed_USGS_global_EQ_catalogue.csv')
    elif ifo == "Virgo":
        trainFile = os.path.join(scriptpath,'LHO_processed_USGS_global_EQ_catalogue.csv')
    else:
        trainFile = os.path.join(scriptpath,'LHO_processed_USGS_global_EQ_catalogue.csv')

    trainData = pd.read_csv(trainFile)

    thresh=0.1
    predictor='peak_data_um_mean_subtracted'
    locklossMotionThresh=10*1e-6

    (predicted_peak_amplitude,LocklossTag,Rfamp_sigma,LocklossTag_sigma,TD) = eqmon.make_prediction(trainData,
                    eqlat,
                    eqlon,
                    mag,
                    depth,
                    ifolat,
                    ifolon,
                    thresh,predictor,locklossMotionThresh)
    
    return predicted_peak_amplitude, LocklossTag 


def ingest_ifos():

    ifos = {"LHO": {'Latitude': 46.6475, 'Longitude': -119.5986},
            "LLO": {'Latitude': 30.4986, 'Longitude': -90.7483},
            "GEO": {'Latitude': 52.246944, 'Longitude': 9.80833},
            "VIRGO": {'Latitude': 43.631389, 'Longitude': 10.505},
            "KAGRA": {'Latitude': 36.4119, 'Longitude': 137.3058}}

    for det in ifos.keys():
        DBSession().merge(Ifo(ifo=det,
                              lat=ifos[det]["Latitude"],
                              lon=ifos[det]["Longitude"]))
    DBSession().commit()

def ingest_earthquakes(config, lookback, repeat=False):

# convert lookback to TimeDelta
    lookbackTD = TimeDelta(lookback,format='jd')

    folders = glob.glob(os.path.join(config["pdlcient"]["directory"],"*"))
    for folder in folders:
        folderSplit = folder.split("/")
        eventName = folderSplit[len(folderSplit) - 1]
        dataFolder = os.path.join(folder, eventName[0:2])
        timeFolders = glob.glob(os.path.join(dataFolder,"*"))
        timeFolders = sorted(timeFolders)

        if timeFolders == []:
            continue

        for timeFolder in timeFolders:
            attributeDic = []
            eqxmlfile = os.path.join(timeFolder,"eqxml.xml")
            quakemlfile = os.path.join(timeFolder,"quakeml.xml")

            if not repeat:
                if os.path.isfile(os.path.join(timeFolder,"eqxml.txt")):
                    return

            f = open(os.path.join(timeFolder,"eqxml.txt"),"w")
            f.write("Done")
            f.close()

            if os.path.isfile(eqxmlfile):
                attributeDic = eqmon.read_eqxml(eqxmlfile,eventName)
            elif os.path.isfile(quakemlfile):
                attributeDic = eqmon.read_quakeml(quakemlfile,eventName)

            if attributeDic == []:
                return

            if (not "GPS" in attributeDic) or (not "Magnitude" in attributeDic):
                return

            date = Time(attributeDic["Time"], format='isot', scale='utc') 
            sent = Time(attributeDic["Sent"], format='isot', scale='utc')

            if Time.now() - date > lookbackTD: continue

            eqs = Earthquake.query.filter_by(event_id=attributeDic["eventName"]).all()
            if len(eqs) > 0: continue

            DBSession().merge(Earthquake(depth=attributeDic["Depth"],
                                         lat=attributeDic["Latitude"],
                                         lon=attributeDic["Longitude"],
                                         event_id=attributeDic["eventName"],
                                         magnitude=attributeDic["Magnitude"],
                                         date=date.datetime,
                                         sent=sent.datetime))
            print('Ingested event: %s' % attributeDic["eventName"])
            DBSession().commit()


def run_seismon(purge=False, init_db=False):

    if purge:
        sys_command = "find %s/* -type d -mtime +7 -exec rm -rf {} \;" % config["pdlcient"]["directory"]
        os.system(sys_command)

    if init_db:
        ingest_earthquakes(config, args.lookback, repeat=True)
    else:
        ingest_earthquakes(config, args.lookback)

    ifos = Ifo.query.all()
    eqs = Earthquake.query.all()

    for eq in eqs:
        for det in ifos:
            preds = Prediction.query.filter_by(event_id=eq.event_id,
                                               ifo=det.ifo).all()
            if len(preds) == 0:
                compute_predictions(eq, det)
            pred = Prediction.query.filter_by(event_id=eq.event_id,
                                              ifo=det.ifo).one()
            print(pred)

if __name__ == "__main__":

    from argparse import ArgumentParser
    parser = ArgumentParser()

    parser.add_argument('-i', '--init_db', action='store_true', default=False)
    parser.add_argument('-p', '--purge', action='store_true', default=False)
    parser.add_argument('-C', '--config', default='input/config.yaml')
    parser.add_argument('-l', '--lookback', default=7, help='lookback in days')
    parser.add_argument("-d", "--debug", action="store_true", default=False)

    args = parser.parse_args()

    if not os.path.isfile(args.config):
        print('Missing config file: %s' % args.config)
        exit(1)

    config = configparser.ConfigParser()
    config.read(args.config)

    conn = init_db(config['database']['user'],
                   config['database']['database'],
                   password=config['database']['password'],
                   host=config['database']['host'],
                   port=config['database']['port'])

    if args.init_db:
        print(f'Creating tables on database {conn.url.database}')
        Base.metadata.drop_all()
        Base.metadata.create_all()

        print('Refreshed tables:')
        for m in Base.metadata.tables:
            print(f' - {m}')
        ingest_ifos()

    if args.debug:
        run_seismon(purge=args.purge, init_db=args.init_db)
        exit(0)

    while True:
        #try:
        print('Looking for some earthquakes to analyze!')
        run_seismon(purge=args.purge, init_db=args.init_db)
        #except:
        #    pass
        time.sleep(15)
