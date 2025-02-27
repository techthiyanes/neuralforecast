# AUTOGENERATED! DO NOT EDIT! File to edit: ../nbs/tsdataset.ipynb.

# %% auto 0
__all__ = ['TimeSeriesLoader', 'TimeSeriesDataset', 'TimeSeriesDataModule']

# %% ../nbs/tsdataset.ipynb 4
from collections.abc import Mapping

import numpy as np
import pandas as pd
import pytorch_lightning as pl
import torch
from torch.utils.data import Dataset, DataLoader

# %% ../nbs/tsdataset.ipynb 5
class TimeSeriesLoader(DataLoader):
    """TimeSeriesLoader DataLoader.
    [Source code](https://github.com/Nixtla/neuralforecast1/blob/main/neuralforecast/tsdataset.py).

    Small change to PyTorch's Data loader. 
    Combines a dataset and a sampler, and provides an iterable over the given dataset.

    The class `~torch.utils.data.DataLoader` supports both map-style and
    iterable-style datasets with single- or multi-process loading, customizing
    loading order and optional automatic batching (collation) and memory pinning.    
    
    **Parameters:**<br>
    `batch_size`: (int, optional): how many samples per batch to load (default: 1).<br>
    `shuffle`: (bool, optional): set to `True` to have the data reshuffled at every epoch (default: `False`).<br>
    `sampler`: (Sampler or Iterable, optional): defines the strategy to draw samples from the dataset.<br>
                Can be any `Iterable` with `__len__` implemented. If specified, `shuffle` must not be specified.<br>
    """
    def __init__(self, dataset, **kwargs):
        if 'collate_fn' in kwargs:
            kwargs.pop('collate_fn')
        kwargs_ = {**kwargs, **dict(collate_fn=self._collate_fn)}
        DataLoader.__init__(self, dataset=dataset, **kwargs_)
    
    def _collate_fn(self, batch):
        elem = batch[0]
        elem_type = type(elem)

        if isinstance(elem, torch.Tensor):
            out = None
            if torch.utils.data.get_worker_info() is not None:
                # If we're in a background process, concatenate directly into a
                # shared memory tensor to avoid an extra copy
                numel = sum(x.numel() for x in batch)
                storage = elem.storage()._new_shared(numel, device=elem.device)
                out = elem.new(storage).resize_(len(batch), *list(elem.size()))
            return torch.stack(batch, 0, out=out)

        elif isinstance(elem, Mapping):
            if elem['static'] is None:
                return dict(temporal=self.collate_fn([d['temporal'] for d in batch]),
                            temporal_cols = elem['temporal_cols'])
            
            return dict(static=self.collate_fn([d['static'] for d in batch]),
                        static_cols = elem['static_cols'],
                        temporal=self.collate_fn([d['temporal'] for d in batch]),
                        temporal_cols = elem['temporal_cols'])

        raise TypeError(f'Unknown {elem_type}')

# %% ../nbs/tsdataset.ipynb 7
class TimeSeriesDataset(Dataset):

    def __init__(self,
                 temporal,
                 temporal_cols,
                 indptr,
                 max_size,
                 static=None,
                 static_cols=None,
                 sorted=False):
        super().__init__()

        self.temporal = torch.tensor(temporal, dtype=torch.float)
        self.temporal_cols = pd.Index(list(temporal_cols)+\
                                      ['available_mask'])
        if static is not None:
            self.static = torch.tensor(static, dtype=torch.float)
            self.static_cols = static_cols
        else:
            self.static = static
            self.static_cols = static_cols

        self.indptr = indptr
        self.n_groups = self.indptr.size - 1
        self.max_size = max_size

        # Upadated flag. To protect consistency, dataset can only be updated once
        self.updated = False
        self.sorted = sorted

    def __getitem__(self, idx):
        if isinstance(idx, int):
            # Parse temporal data and pad its left
            temporal = torch.zeros(size=(len(self.temporal_cols), self.max_size),
                                   dtype=torch.float32)
            ts = self.temporal[self.indptr[idx] : self.indptr[idx + 1], :]
            temporal[:len(self.temporal_cols)-1, -len(ts):] = ts.permute(1, 0)

            # Add available_mask
            temporal[len(self.temporal_cols)-1, -len(ts):] = 1

            # Add static data if available
            static = None if self.static is None else self.static[idx,:]

            item = dict(temporal=temporal, temporal_cols=self.temporal_cols,
                        static=static, static_cols=self.static_cols)

            return item
        raise ValueError(f'idx must be int, got {type(idx)}')

    def __len__(self):
        return self.n_groups

    def __repr__(self):
        return f'TimeSeriesDataset(n_data={self.data.size:,}, n_groups={self.n_groups:,})'

    def __eq__(self, other):
        if not hasattr(other, 'data') or not hasattr(other, 'indptr'):
            return False
        return np.allclose(self.data, other.data) and np.array_equal(self.indptr, other.indptr)

    @staticmethod
    def update_dataset(dataset, future_df):
        """Add future observations to the dataset.
        """        

        # Add Nones to missing columns (without available_mask)
        temporal_cols = dataset.temporal_cols.copy()
        temporal_cols = temporal_cols.delete(len(temporal_cols)-1)
        for col in temporal_cols:
            if col not in future_df.columns:
                future_df[col] = None
        
        # Sort columns to match self.temporal_cols
        future_df = future_df[ ['unique_id','ds'] + temporal_cols.tolist() ]

        # Process future_df
        futr_dataset, indices, futr_dates, futr_index = dataset.from_df(df=future_df, sort_df=dataset.sorted)

        # Define and fill new temporal with updated information
        len_temporal, col_temporal = dataset.temporal.shape
        new_temporal = torch.zeros(size=(len_temporal+len(future_df), col_temporal))
        new_indptr = [0]
        new_max_size = 0

        acum = 0
        for i in range(dataset.n_groups):
            series_length = dataset.indptr[i + 1] - dataset.indptr[i]
            new_length = series_length + futr_dataset.indptr[i + 1] - futr_dataset.indptr[i]
            new_temporal[acum:(acum+series_length), :] = dataset.temporal[dataset.indptr[i] : dataset.indptr[i + 1], :]
            new_temporal[(acum+series_length):(acum+new_length), :] = \
                                 futr_dataset.temporal[futr_dataset.indptr[i] : futr_dataset.indptr[i + 1], :]
            
            acum += new_length
            new_indptr.append(acum)
            if new_length > new_max_size:
                new_max_size = new_length
        
        # Define new dataset
        updated_dataset = TimeSeriesDataset(temporal=new_temporal,
                                            temporal_cols=temporal_cols,
                                            indptr=np.array(new_indptr).astype(np.int32),
                                            max_size=new_max_size,
                                            static=dataset.static,
                                            static_cols=dataset.static_cols,
                                            sorted=dataset.sorted)

        return updated_dataset

    @staticmethod
    def from_df(df, static_df=None, sort_df=False):
        # TODO: protect on equality of static_df + df indexes
        # Define indexes if not given
        if df.index.name != 'unique_id':
            df = df.set_index('unique_id')
            if static_df is not None:
                static_df = static_df.set_index('unique_id')

        df = df.set_index('ds', append=True)
        
        # Sort data by index
        if not df.index.is_monotonic_increasing and sort_df:
            df = df.sort_index()

            if static_df is not None:
                static_df = static_df.sort_index()

        # Create auxiliary temporal indices 'indptr'
        temporal = df.values.astype(np.float32)
        temporal_cols = df.columns
        indices_sizes = df.index.get_level_values('unique_id').value_counts(sort=False)
        indices = indices_sizes.index
        sizes = indices_sizes.values
        max_size = max(sizes)
        cum_sizes = sizes.cumsum()
        dates = df.index.get_level_values('ds')[cum_sizes - 1]
        indptr = np.append(0, cum_sizes).astype(np.int32)

        # Static features
        if static_df is not None:
            static = static_df.values
            static_cols = static_df.columns
        else:
            static = None
            static_cols = None

        dataset = TimeSeriesDataset(
                    temporal=temporal, temporal_cols=temporal_cols,
                    static=static, static_cols=static_cols,
                    indptr=indptr, max_size=max_size, sorted=sort_df)
        return dataset, indices, dates, df.index

# %% ../nbs/tsdataset.ipynb 10
class TimeSeriesDataModule(pl.LightningDataModule):
    
    def __init__(
            self, 
            dataset: TimeSeriesDataset,
            batch_size=32, 
            num_workers=0,
            drop_last=False
        ):
        super().__init__()
        self.dataset = dataset
        self.batch_size = batch_size
        self.num_workers = num_workers
        self.drop_last = drop_last
    
    def train_dataloader(self):
        loader = TimeSeriesLoader(
            self.dataset, 
            batch_size=self.batch_size, 
            num_workers=self.num_workers,
            shuffle=True,
            drop_last=self.drop_last
        )
        return loader
    
    def val_dataloader(self):
        loader = TimeSeriesLoader(
            self.dataset, 
            batch_size=self.batch_size, 
            num_workers=self.num_workers,
            shuffle=False,
            drop_last=self.drop_last
        )
        return loader
    
    def predict_dataloader(self):
        loader = TimeSeriesLoader(
            self.dataset,
            batch_size=self.batch_size, 
            num_workers=self.num_workers,
            shuffle=False
        )
        return loader
