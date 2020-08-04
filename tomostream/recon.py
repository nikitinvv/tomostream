import pvaccess as pva
import numpy as np
import time

from tomostream import util
from tomostream import log
from tomostream import pv
from tomostream import solver


class Recon():
    """ Class for operating with pv variables and running streaming reconstuction.

        Parameters
        ----------
        args : dict
            Dictionary of pv variables.
    """

    def __init__(self, args):

        ts_pvs = pv.init(args.tomoscan_prefix)  # read all pvs

        # pva type channel that contains projection and metadata
        ch_data = ts_pvs['chData']
        # pva type channel for flat and dark fields pv broadcasted from the detector machine
        ch_flat_dark = ts_pvs['chFlatDark']

        # create pva type pv for reconstrucion by copying metadata from the data pv, but replacing the sizes
        pv_data = ch_data.get('')
        pv_dict = pv_data.getStructureDict()
        width = pv_data['dimension'][0]['size']
        height = pv_data['dimension'][1]['size']
        datatype_list = ts_pvs['chDataType_RBV'].get('')['value']
        self.datatype = datatype_list['choices'][datatype_list['index']].lower(
        )
        self.pv_rec = pva.PvObject(pv_dict)
        # set dimensions for reconstruction (assume width>=height), todo if not
        self.pv_rec['dimension'] = [{'size': 3*width, 'fullSize': 3*width, 'binning': 1},
                                    {'size': width, 'fullSize': width, 'binning': 1}]

        # run server for reconstruction pv
        self.server_rec = pva.PvaServer(args.recon_pva_name, self.pv_rec)
        log.info('Reconstruction PV: %s, size: %s %s',
                 args.recon_pva_name, 3*width, width)

        # form circular buffers, whenever the projection count goes higher than buffer_size
        # then corresponding projection is replacing the first one
        buffer_size = ts_pvs['chStreamBufferSize'].get('')['value']
        # read initial parameters from the GUI
        center = ts_pvs['chStreamCenter'].get('')['value']
        idx = ts_pvs['chStreamOrthoX'].get('')['value']
        idy = ts_pvs['chStreamOrthoY'].get('')['value']
        idz = ts_pvs['chStreamOrthoZ'].get('')['value']
        
        self.proj_buffer = np.zeros(
            [buffer_size, width*height], dtype=self.datatype)
        self.theta_buffer = np.zeros(buffer_size, dtype='float32')
        self.ids_buffer = np.zeros(buffer_size, dtype='int32')

        # load angles
        self.theta = ts_pvs['chStreamThetaArray'].get(
            '')['value'][:ts_pvs['chStreamNumAngles'].get('')['value']]

        
        # create solver class on GPU
        self.slv = solver.Solver(buffer_size, width, height, center, idx, idy, idz)

        # parameters needed in other class functions
        self.ts_pvs = ts_pvs
        self.width = width
        self.height = height
        self.buffer_size = buffer_size
        self.num_proj = 0

        # start monitoring projection data
        ch_data.monitor(self.add_data, '')
        # start monitoring dark and flat fields pv
        ch_flat_dark.monitor(self.add_flat_dark, '')

    def add_data(self, pv):
        """PV monitoring function for adding projection data and corresponding angle to a circular buffer"""
        if(self.ts_pvs['chStreamStatus'].get('')['value']['index'] == 1):
            cur_id = pv['uniqueId']
            frame_type_all = self.ts_pvs['chStreamFrameType'].get('')['value']
            frame_type = frame_type_all['choices'][frame_type_all['index']]
            if(frame_type == 'Projection'):
                # write projection to a buffer
                self.proj_buffer[np.mod(self.num_proj, self.buffer_size)
                                 ] = pv['value'][0][util.type_dict[self.datatype]]
                # write theta to a buffer
                self.theta_buffer[np.mod(
                    self.num_proj, self.buffer_size)] = self.theta[cur_id]
                # write position in the projection buffer
                self.ids_buffer[np.mod(
                    self.num_proj, self.buffer_size)] = np.mod(self.num_proj, self.buffer_size)
                self.num_proj += 1
                log.info('id: %s type %s', cur_id, frame_type)

    def add_flat_dark(self, pv):
        """PV monitoring function for reading new flat and dark fields from the manually running pv server on the detector machine"""
        if(pv['value'][0]):  # if pv with dark and flat is not empty
            dark_flat = pv['value'][0]['floatValue']
            num_flat_fields = self.ts_pvs['chStreamNumFlatFields'].get(
                '')['value']  # probably better to read from the server pv
            num_dark_fields = self.ts_pvs['chStreamNumDarkFields'].get('')[
                'value']
            dark = dark_flat[:num_dark_fields * self.width*self.height]
            flat = dark_flat[num_dark_fields * self.width*self.height:]
            dark = dark.reshape(num_dark_fields, self.height, self.width)
            flat = flat.reshape(num_flat_fields, self.height, self.width)
            # send dark and flat fields to the solver
            self.slv.set_dark(dark)
            self.slv.set_flat(flat)
            log.info('new flat and dark fields acquired')

    def run(self):
        """Run streaming reconstruction by sending new incoming projections from the circular buffer to the solver class,
        and broadcasting the reconstruction result to a pv variable
        """
        id_start = 0  # start position in the circular buffer
                
        while(True):
            # if streaming status is on
            if(self.ts_pvs['chStreamStatus'].get('')['value']['index'] == 1):
                # take positions of new projections in the buffer
                ids = np.mod(np.arange(id_start, self.num_proj),
                             self.buffer_size)
                # update id_start to the projection part
                id_start = self.num_proj

                if(len(ids) == 0):  # if no new data in the buffer then continue
                    continue                
                # take parameters from the GUI
                if(len(ids) > self.buffer_size):
                    # recompute if the buffer was overfilled, 
                    ids = np.arange(self.buffer_size)                    
                
                center = self.ts_pvs['chStreamCenter'].get('')['value']
                idx = self.ts_pvs['chStreamOrthoX'].get('')['value']
                idy = self.ts_pvs['chStreamOrthoY'].get('')['value']
                idz = self.ts_pvs['chStreamOrthoZ'].get('')['value']
        
                log.info('center %s: idx, idy, idz: %s %s %s, ids: %s',
                         center, idx, idy, idz, len(ids))
                                
                                
                # make copies of what should be processed
                proj_part = self.proj_buffer[ids].copy()
                theta_part = self.theta_buffer[ids].copy()
                ids_part = self.ids_buffer[ids].copy()
                
                # reconstruct on GPU
                util.tic()
                rec = self.slv.recon_optimized(
                    proj_part, theta_part, ids_part, center, idx, idy, idz)
                log.info('rec time: %s', util.toc())

                # write to pv
                self.pv_rec['value'] = ({'floatValue': rec.flatten()},)

                # reconstruction rate limit
                time.sleep(0.01)
