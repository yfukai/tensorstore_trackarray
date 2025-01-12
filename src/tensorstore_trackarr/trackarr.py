import tensorstore as ts
import numpy as np
from typing import Sequence
from numpy import typing as npt
import pandas as pd
from skimage.measure import regionprops_table


def to_bbox_df(label: npt.ArrayLike) -> pd.DataFrame:
    bbox_df = pd.concat([pd.DataFrame(regionprops_table(
        np.array(label[frame]), 
        properties=('label', 'bbox'))).assign(frame=frame) \
        for frame in range(label.shape[0])]).set_index(['frame', 'label'])
    # bboxtuple 
    # Bounding box (min_row, min_col, max_row, max_col). 
    # Pixels belonging to the bounding box are in the half-open interval [min_row; max_row) and [min_col; max_col).
    if bbox_df.empty:
        return bbox_df
    bbox_df['min_y'] = bbox_df['bbox-0']
    bbox_df['min_x'] = bbox_df['bbox-1']
    bbox_df['max_y'] = bbox_df['bbox-2']
    bbox_df['max_x'] = bbox_df['bbox-3']
    del bbox_df['bbox-0'], bbox_df['bbox-1'], bbox_df['bbox-2'], bbox_df['bbox-3']
    return bbox_df

class TrackArr:
    def __init__(self, ts_array, splits, termination_annotations, bboxes_df=None):
        self.array = ts_array
        if bboxes_df is None:
            bboxes_df = to_bbox_df(ts_array)
        self.bboxes_df = bboxes_df
        self.update_track_df()
        self.splits = splits
        self.termnation_annotations = termination_annotations
        
    def update_track_df(self):
        self._track_df = self.bboxes_df.reset_index().groupby("label")["frame"].agg(["min","max"])
    
    def validate(self):
        _bboxes_df = to_bbox_df(self.array)
        assert _bboxes_df.sort_index().equals(self.bboxes_df.sort_index())
        
    def _get_track_bboxes(self, trackid: int):
        return self.bboxes_df[self.bboxes_df.index.get_level_values("label") == trackid]
    
    def _get_safe_track_id(self):
        return self.bboxes_df.index.get_level_values("label").max() + 1
        
    def _update_trackid(self, frame: int, trackid: int, new_trackid: int, txn: ts.Transaction, skip_update=False):
        array_txn = self.array.with_transaction(txn)
        row = self.bboxes_df.loc[(frame, trackid)]
        subarr = array_txn[frame, row.min_y:row.max_y, row.min_x:row.max_x]
        ind = np.array(subarr) == trackid
        subarr[ts.d[:].translate_to[0]][ind] = new_trackid
        self.bboxes_df.index = self.bboxes_df.index.map(lambda x: (frame, new_trackid) if x == (frame, trackid) else x)
        
        if not skip_update:
            self.update_track_df()
        
    def swap_tracks(self, trackid1: int, trackid2:int, txn: ts.Transaction):
        array_txn = self.array.with_transaction(txn)
        
        # Find the bboxes of the two tracks
        bboxes_df1 = self._get_track_bboxes(trackid1).reset_index()
        bboxes_df2 = self._get_track_bboxes(trackid2).reset_index()
        
        # Store regions of the two tracks
        bboxes_dfs = [bboxes_df1, bboxes_df2]
        indss = []
        for _bboxes_df, trackid in zip(bboxes_dfs,[trackid1,trackid2]):
            inds = []
            for row in _bboxes_df.itertuples():
                subarr = array_txn[row.frame, row.min_y:row.max_y, row.min_x:row.max_x]
                inds.append(np.array(subarr) == trackid)
            indss.append(inds)

        # Swap the regions
        for _bboxes_df, _inds, new_trackid in zip(bboxes_dfs, indss,[trackid2,trackid1]):
            for row, ind in zip(_bboxes_df.itertuples(), _inds):
                array_txn[row.frame, row.min_y:row.max_y, row.min_x:row.max_x][ts.d[:].translate_to[0]][ind] = new_trackid
        
        # Update bboxes_df
        self.bboxes_df = self.bboxes_df.rename(index={trackid1: trackid2, trackid2: trackid1}, level='label')
        self.update_track_df()
        
        # Update splits
        for parent, daughters in self.splits.items():
            if parent == trackid1:
                _parent = trackid2
                del self.splits[parent]
            elif parent == trackid2:
                _parent = trackid1
                del self.splits[parent]
            else:
                _parent = parent
            _daughters = []
            for daughter in daughters:
                if daughter == trackid1:
                    _daughters.append(trackid2)
                elif daughter == trackid2:
                    _daughters.append(trackid1)
                else:
                    _daughters.append(daughter)
            self.splits[_parent] = _daughters
            
        # Update termination_annotations
        tas = [self.termnation_annotations.pop(trackid2, None), 
               self.termnation_annotations.pop(trackid1, None)]
        if tas[0] is not None:
            self.termnation_annotations[trackid1] = tas[0]
        if tas[1] is not None:
            self.termnation_annotations[trackid2] = tas[1]
        
    def delete_mask(self, frame: int, trackid: int, txn: ts.Transaction, skip_update=False, cleanup=True):
        row = self.bboxes_df.loc[(frame, trackid)]
        array_txn = self.array.with_transaction(txn)
        ind = np.array(array_txn[frame, row.min_y:row.max_y, row.min_x:row.max_x]) == trackid
        array_txn[frame, row.min_y:row.max_y, row.min_x:row.max_x][ts.d[:].translate_to[0]][ind] = 0
        self.bboxes_df.drop(index=(frame, trackid), inplace=True)
        if not skip_update:
            self.update_track_df()

        if cleanup and self._get_track_bboxes(trackid).empty: # if the track becomes empty
            self.termnation_annotations.pop(trackid, None)
            self.splits.pop(trackid, None)
            for parent, daughters in self.splits.copy().items():
                self.splits[parent] = [daughter for daughter in daughters if daughter != trackid]
            self.cleanup_single_daughter_splits()
        
    def add_mask(self, frame: int, trackid:int, mask_origin: Sequence[int], mask, txn: ts.Transaction):
        assert mask.shape[0] + mask_origin[0] <= self.array.shape[1]
        assert mask.shape[1] + mask_origin[1] <= self.array.shape[2]
        assert mask.dtype == bool
        array_txn = self.array.with_transaction(txn)
        inds = np.where(mask)
        mask_min_y, mask_min_x = np.min(inds, axis=1)
        mask_max_y, mask_max_x = np.max(inds, axis=1)
        y_window = (mask_origin[0] + mask_min_y, mask_origin[0] + mask_max_y + 1)
        x_window = (mask_origin[1] + mask_min_x, mask_origin[1] + mask_max_x + 1)        
        mask2 = mask[mask_min_y:mask_max_y+1, mask_min_x:mask_max_x+1]
        array_txn[frame, y_window[0]:y_window[1], x_window[0]:x_window[1]][ts.d[:].translate_to[0]][mask2] = trackid
        
        self.bboxes_df = pd.concat([self.bboxes_df, pd.DataFrame({
            'min_y':y_window[0],
            'min_x':x_window[0],
            'max_y':y_window[1],
            'max_x':x_window[1],
        },
        index=pd.MultiIndex.from_tuples([(frame, trackid)], names=['frame', 'label']))])
        self.update_track_df()
        
    def update_mask(self, frame: int, trackid:int, new_mask_origin: Sequence[int], new_mask, txn: ts.Transaction):
        self.delete_mask(frame, trackid, txn, cleanup=False, skip_update=True)
        self.add_mask(frame, trackid, new_mask_origin, new_mask, txn)
        
    def terminate_track(self, frame:int, trackid: int, annotation:str, txn: ts.Transaction):
        bboxes_df = self._get_track_bboxes(trackid).reset_index()
        bboxes_df = bboxes_df[bboxes_df.frame > frame]
        for frame in bboxes_df.frame:
            self.delete_mask(frame, trackid, txn, skip_update=True)
        self.update_track_df()
        self.termnation_annotations[trackid] = annotation
        self.splits.pop(trackid, None)
    
    def break_track(self, new_start_frame:int, trackid: int, change_after, txn: ts.Transaction):
        safe_track_id = self._get_safe_track_id()
        bboxes_df = self._get_track_bboxes(trackid).reset_index()
        if change_after:
            bboxes_df = bboxes_df[bboxes_df.frame >= new_start_frame]
        else:
            bboxes_df = bboxes_df[bboxes_df.frame < new_start_frame]
        for frame in bboxes_df.frame:
            self._update_trackid(frame, trackid, safe_track_id, txn, skip_update=True)
        self.update_track_df()

        if change_after:
            # Update splits
            if trackid in self.splits:
                daughters = self.splits.pop(trackid)
                self.splits[safe_track_id] = daughters
            # Update termination_annotations
            if trackid in self.termnation_annotations:
                self.termnation_annotations[safe_track_id] = self.termnation_annotations.pop(trackid)
        else:
            # Update splits
            for parent, daughters in self.splits.copy().items():
                if trackid in daughters:
                    daughters.remove(trackid)
                    daughters.append(safe_track_id)
                    self.splits[parent] = daughters
                    
        return safe_track_id
    
    def add_split(self, daughter_start_frame:int, parent_trackid, daughter_trackids, txn: ts.Transaction):
        new_track_id = self.break_track(daughter_start_frame, parent_trackid, change_after=True, txn=txn)
        if parent_trackid in daughter_trackids:
            daughter_trackids.remove(parent_trackid)
            daughter_trackids.append(new_track_id)
            
        for daughter_trackid in daughter_trackids:
            self.break_track(daughter_start_frame, daughter_trackid, change_after=False, txn=txn)
            for parent, daughters in self.splits.copy().items():
                if daughter_trackid in daughters:
                    daughters.remove(daughter_trackid)
                    self.splits[parent] = daughters
            
        self.splits[parent_trackid] = daughter_trackids
        self.cleanup_single_daughter_splits()
    
    def cleanup_single_daughter_splits(self):
        for parent, daughters in self.splits.copy().items():
            if len(daughters) == 1:
                daughter = daughters[0]
                track_df = self._get_track_bboxes(daughter).reset_index()
                for frame in track_df.frame:
                    self._update_trackid(frame, daughter, parent, None, skip_update=True)
                self.splits.pop(parent)