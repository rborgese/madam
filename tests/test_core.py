import unittest.mock

import io
import os
import pytest
import tempfile

from madam.core import Asset
from madam.core import InMemoryStorage, ShelveStorage
from madam.core import Pipeline


@pytest.fixture
def in_memory_storage():
    return InMemoryStorage()


@pytest.fixture
def shelve_storage(tmpdir):
    storage_path = str(tmpdir.join('shelf'))
    return ShelveStorage(storage_path)


@pytest.mark.usefixtures('asset', 'in_memory_storage', 'shelve_storage')
class TestStorages:
    @pytest.fixture(params=['in_memory_storage', 'shelve_storage'])
    def storage(self, request, in_memory_storage, shelve_storage):
        if request.param == 'in_memory_storage':
            return in_memory_storage
        elif request.param == 'shelve_storage':
            return shelve_storage

    def test_contains_is_false_when_storage_is_empty(self, storage, asset):
        contains = asset in storage

        assert not contains

    def test_contains_is_true_when_asset_was_added(self, storage, asset):
        storage.add(asset)

        assert asset in storage

    def test_contains_is_false_when_asset_was_deleted(self, storage, asset):
        storage.add(asset)

        storage.remove(asset)

        assert asset not in storage

    def test_remove_raises_value_error_when_deleting_unknown_asset(self, storage, asset):
        with pytest.raises(ValueError):
            storage.remove(asset)

    def test_remove_deletes_asset_from_storage(self, storage, asset):
        storage.add(asset)

        storage.remove(asset)

        assert asset not in storage

    def test_iterator_contains_all_stored_assets(self, storage):
        storage.add(Asset(io.BytesIO(b'0')))
        storage.add(Asset(io.BytesIO(b'1')))
        storage.add(Asset(io.BytesIO(b'2')))

        iterator = iter(storage)

        assert len(list(iterator)) == 3

    def test_iterator_is_a_readable_storage_snapshot(self, storage):
        asset0 = Asset(io.BytesIO(b'0'))
        asset1 = Asset(io.BytesIO(b'1'))
        storage.add(asset0)
        storage.add(asset1)
        iterator = iter(storage)

        storage.remove(asset0)
        storage.add(Asset(io.BytesIO(b'2')))
        storage.add(Asset(io.BytesIO(b'3')))

        assert set(iterator) == {asset0, asset1}

    def test_get_tags_returns_tags_for_asset(self, storage, asset):
        asset_tags = {'foo', 'bar'}
        storage.add(asset, tags=asset_tags)

        tags = storage.get_tags(asset)

        assert tags == asset_tags

    def test_get_tags_fails_for_unknown_asset(self, storage):
        unstored_asset = None

        with pytest.raises(KeyError):
            storage.get_tags(unstored_asset)

    def test_filter_by_tags_returns_empty_iterator_when_storage_is_empty(self, storage):
        tagged_assets = storage.filter_by_tags('some tag')

        assert len(list(tagged_assets)) == 0

    def test_filter_by_tags_returns_all_assets_when_no_tags_are_specified(self, storage, asset):
        storage.add(asset, tags={'foo'})

        assets = storage.filter_by_tags()

        assert asset in assets

    def test_filter_by_tags_returns_assets_with_specified_tags(self, storage):
        asset0 = Asset(io.BytesIO(b'0'))
        asset1 = Asset(io.BytesIO(b'1'))
        asset2 = Asset(io.BytesIO(b'2'))
        storage.add(asset0, tags={'foo'})
        storage.add(asset1, tags={'foo', 'bar'})
        storage.add(asset2, tags={'foo', 'bar'})

        assets = list(storage.filter_by_tags('bar', 'foo'))

        assert asset0 not in assets and asset1 in assets and asset2 in assets

    @pytest.mark.parametrize('tags', [None, {'my', 'tags'}])
    def test_add_does_nothing_when_asset_is_already_in_storage(self, storage, asset, tags):
        storage.add(asset, tags=tags)

        storage.add(asset, tags=tags)

        assert len(list(storage)) == 1


@pytest.mark.usefixtures('asset', 'shelve_storage')
class TestShelveStorage:
    @pytest.fixture
    def storage(self, shelve_storage):
        return shelve_storage

    def test_raises_error_when_storage_path_is_not_a_file(self, tmpdir):
        with pytest.raises(ValueError):
            ShelveStorage(str(tmpdir))

    def test_add_writes_data_to_storage_path(self, storage, asset):
        storage.add(asset)

        assert os.path.exists(storage.path)


@pytest.mark.usefixtures('in_memory_storage')
class TestInMemoryStorage:
    @pytest.fixture
    def storage(self, in_memory_storage):
        return in_memory_storage

    def test_get_returns_empty_list_when_storage_is_empty(self, storage):
        assets_with_1s_duration = storage.get()
        assert not assets_with_1s_duration

    def test_get_returns_assets_with_specified_madam_metadata(self, storage):
        asset_with_metadata = Asset(io.BytesIO(b'TestEssence'), duration=1)
        storage.add(asset_with_metadata)

        assets_with_1s_duration = storage.get(duration=1)

        assert len(assets_with_1s_duration) == 1
        assert assets_with_1s_duration[0] == asset_with_metadata


@pytest.fixture
def asset():
    return Asset(io.BytesIO(b'TestEssence'))


@pytest.mark.usefixtures('asset')
class TestAsset:
    def test_asset_has_mime_type(self, asset):
        assert hasattr(asset, 'mime_type')

    def test_asset_has_essence(self, asset):
        assert hasattr(asset, 'essence')

    def test_asset_has_metadata(self, asset):
        assert hasattr(asset, 'metadata')

    def test_assets_are_equal_when_essence_and_properties_are_identical(self, asset):
        asset.some_attr = 42
        another_asset = Asset(asset.essence)
        another_asset.some_attr = 42

        assert asset is not another_asset
        assert asset == another_asset

    def test_asset_getattr_is_identical_to_access_through_metadata(self):
        asset_with_metadata = Asset(io.BytesIO(b'TestEssence'), SomeKey='SomeValue', AnotherKey=None, _42=43.0)

        for key, value in asset_with_metadata.metadata.items():
            assert getattr(asset_with_metadata, key) == value

    def test_setattr_raises_when_attribute_is_a_metadata_attribute(self):
        asset_with_metadata = Asset(io.BytesIO(b''), SomeMetadata=42)

        with pytest.raises(NotImplementedError):
            asset_with_metadata.SomeMetadata = 43


    def test_asset_essence_can_be_read_multiple_times(self, asset):
        essence_contents = asset.essence.read()
        same_essence_contents = asset.essence.read()

        assert essence_contents == same_essence_contents

    def test_hash_is_equal_for_equal_assets(self):
        metadata = dict(SomeMetadata=42)
        asset0 = Asset(io.BytesIO(b'same'), **metadata)
        asset1 = Asset(io.BytesIO(b'same'), **metadata)

        assert hash(asset0) == hash(asset1)

    def test_hash_is_different_when_assets_have_different_metadata(self):
        asset0 = Asset(io.BytesIO(b'same'), SomeMetadata=42)
        asset1 = Asset(io.BytesIO(b'same'), DifferentMetadata=43)

        assert hash(asset0) != hash(asset1)


@pytest.mark.usefixtures('asset')
class TestPipeline:
    @pytest.fixture
    def pipeline(self):
        return Pipeline()

    def test_empty_pipeline_does_not_change_assets(self, pipeline):
        some_asset = Asset(io.BytesIO(b'some'))
        another_asset = Asset(io.BytesIO(b'other'))

        processed_assets = pipeline.process(some_asset, another_asset)

        assert some_asset in processed_assets
        assert another_asset in processed_assets

    def test_pipeline_contains_operator_after_it_was_added(self, pipeline):
        operator = unittest.mock.MagicMock()

        pipeline.add(operator)

        assert operator in pipeline.operators

    def test_operator_is_applied_to_assets_when_process_is_called(self, pipeline, asset):
        operator = unittest.mock.MagicMock()
        pipeline.add(operator)

        [processed_asset for processed_asset in pipeline.process(asset)]

        operator.assert_called_once_with(asset)
